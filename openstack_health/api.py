# Copyright 2015 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.


import argparse
from contextlib import contextmanager
import datetime
from dateutil import parser as date_parser
import itertools
import numpy
import os
from six.moves import configparser as ConfigParser
from six.moves.urllib import parse
import tempfile
import threading

import dogpile.cache
from feedgen import feed
import flask
from flask import abort
from flask import make_response
from flask import request
from flask_jsonpify import jsonify
from operator import itemgetter
from pbr import version
import pyelasticsearch
import pytz
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from subunit2sql.db import api

from openstack_health import distributed_dbm
from openstack_health import run_aggregator
from openstack_health import test_run_aggregator

try:
    from elastic_recheck import config as er_config
    from elastic_recheck import elasticRecheck as er
except ImportError:
    er = None

app = flask.Flask(__name__)
app.config['PROPAGATE_EXCEPTIONS'] = True
config = None
engine = None
Session = None
query_dir = None
classifier = None
rss_opts = {}
feeds = {'last runs': {}}
region = None
es_url = None


def get_app():
    return app


def _config_get(config_func, section, option, default_val=False):
    retval = default_val
    if default_val is not False:
        try:
            retval = config_func(section, option)
        except ConfigParser.NoOptionError:
            pass
    else:
        retval = config_func(section, option)
    return retval


@app.before_first_request
def _setup():
    setup()


def setup():
    global config
    if not config:
        args = parse_command_line_args()
        config = ConfigParser.ConfigParser()
        config.read(args.config_file)
    # Database Configuration
    global engine
    db_uri = _config_get(config.get, 'default', 'db_uri')
    pool_size = _config_get(config.getint, 'default', 'pool_size', 20)
    pool_recycle = _config_get(config.getint, 'default', 'pool_recycle', 3600)
    engine = create_engine(db_uri,
                           pool_size=pool_size,
                           pool_recycle=pool_recycle)
    global Session
    Session = sessionmaker(bind=engine)
    # RSS Configuration
    rss_opts['frontend_url'] = _config_get(
        config.get, 'default', 'frontend_url',
        'http://status.openstack.org/openstack-health')
    # Elastic-recheck Configuration
    global query_dir
    query_dir = _config_get(config.get, 'default', 'query_dir', None)
    global es_url
    es_url = _config_get(config.get, 'default', 'es_url', None)
    if query_dir and er:
        elastic_config = er_config.Config(es_url=es_url)
        global classifier
        classifier = er.Classifier(query_dir, config=elastic_config)
    # Cache Configuration
    backend = _config_get(config.get, 'default', 'cache_backend',
                          'dogpile.cache.dbm')
    expire = _config_get(config.getint, 'default', 'cache_expiration',
                         datetime.timedelta(minutes=30))
    cache_file = _config_get(config.get, 'default', 'cache_file',
                             os.path.join(tempfile.gettempdir(),
                                          'openstack-health.dbm'))
    cache_url = _config_get(config.get, 'default', 'cache_url', None)

    global region
    if backend == 'dogpile.cache.dbm':
        args = {'filename': cache_file}
        if cache_url:
            def _key_generator(namespace, fn, **kw):
                namespace = fn.__name__ + (namespace or '')

                def generate_key(*arg):
                    return namespace + "_".join(
                        str(s).replace(' ', '_') for s in arg)
                return generate_key

            memcache_proxy = distributed_dbm.MemcachedLockedDBMProxy(
                cache_url)
            region = dogpile.cache.make_region(
                async_creation_runner=_periodic_refresh_cache,
                function_key_generator=_key_generator).configure(
                    backend, expiration_time=expire, arguments=args,
                    wrap=[memcache_proxy])
        else:
            region = dogpile.cache.make_region().configure(
                backend, expiration_time=expire, arguments=args)
    else:
        args = {'distributed_lock': True}
        if cache_url:
            args['url'] = cache_url
        region = dogpile.cache.make_region(
            async_creation_runner=_periodic_refresh_cache).configure(
                backend, expiration_time=expire, arguments=args)


def get_session():
    global Session
    if not Session:
        setup()
    return Session()


@contextmanager
def session_scope():
    try:
        session = get_session()
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def elastic_recheck_cached(change_num, patch_num, short_uuid):
    global region
    if not region:
        setup()

    @region.cache_on_arguments()
    def _elastic_recheck_cached(change_num, patch_num, short_uuid):
        return classifier.classify(change_num, patch_num,
                                   short_uuid, recent=False)

    return _elastic_recheck_cached(change_num, patch_num, short_uuid)


@app.route('/', methods=['GET'])
def list_routes():
    output = []
    for rule in app.url_map.iter_rules():
        options = {}
        for arg in rule.arguments:
            options[arg] = "[{0}]".format(arg)
        url = flask.url_for(rule.endpoint, **options)
        out_dict = {
            'name': rule.endpoint,
            'methods': sorted(rule.methods),
            'url': parse.unquote(url),
        }
        output.append(out_dict)
    return jsonify({'routes': output})


@app.route('/build_name/<string:build_name>/runs', methods=['GET'])
def get_runs_from_build_name(build_name):
    with session_scope() as session:
        build_name = parse.unquote(build_name)
        db_runs = api.get_runs_by_key_value('build_name', build_name, session)
        runs = [run.to_dict() for run in db_runs]
        return jsonify({'runs': runs})


@app.route('/runs/metadata/keys', methods=['GET'])
def get_run_metadata_keys():
    global config
    try:
        if config:
            ignored_keys = (config
                            .get('default', 'ignored_run_metadata_keys')
                            .splitlines())
        else:
            ignored_keys = []
    except ConfigParser.NoOptionError:
        ignored_keys = []

    with session_scope() as session:
        existing_keys = set(api.get_all_run_metadata_keys(session))
        allowed_keys = existing_keys.difference(ignored_keys)

        return jsonify(list(allowed_keys))


def _parse_datetimes(datetime_str):
    if datetime_str:
        return date_parser.parse(datetime_str)
    else:
        return datetime_str


@app.route('/runs/group_by/<string:key>', methods=['GET'])
def get_runs_grouped_by_metadata_per_datetime(key):
    key = parse.unquote(key)
    start_date = _parse_datetimes(flask.request.args.get('start_date', None))
    stop_date = _parse_datetimes(flask.request.args.get('stop_date', None))
    datetime_resolution = flask.request.args.get('datetime_resolution', 'sec')
    with session_scope() as session:
        sec_runs = api.get_all_runs_time_series_by_key(key, start_date,
                                                       stop_date, session)

        if datetime_resolution not in ['sec', 'min', 'hour', 'day']:
            return ('Datetime resolution: %s, is not a valid'
                    ' choice' % datetime_resolution), 400

        runs = run_aggregator.RunAggregator(sec_runs).aggregate(
            datetime_resolution)

        return jsonify({'runs': runs})


def _group_runs_by_key(runs_by_time, groupby_key):
    """
    Groups runs by a key.
    This function assumes that your runs are already grouped by time.
    """

    keyfunc = lambda c: c['metadata'].get(groupby_key)
    grouped_runs_by = {}
    for timestamp, run_by_time in runs_by_time.items():
        if timestamp not in grouped_runs_by:
            grouped_runs_by[timestamp] = {}
        for key, val in itertools.groupby(run_by_time, keyfunc):
            if val:
                grouped_runs_by[timestamp][key] = list(val)
    return grouped_runs_by


@app.route('/build_name/<path:build_name>/test_runs', methods=['GET'])
def get_test_runs_by_build_name(build_name):
    value = parse.unquote(build_name)
    if not value:
        return 'A build name must be specified', 400
    start_date = _parse_datetimes(flask.request.args.get('start_date', None))
    stop_date = _parse_datetimes(flask.request.args.get('stop_date', None))
    datetime_resolution = flask.request.args.get('datetime_resolution', 'sec')
    if datetime_resolution not in ['sec', 'min', 'hour', 'day']:
        return ('Datetime resolution: %s, is not a valid'
                ' choice' % datetime_resolution), 400

    @region.cache_on_arguments()
    def _query_test_runs_by_build_name(name, start_date, stop_date):
        with session_scope() as session:
            tests = api.get_test_run_dict_by_run_meta_key_value('build_name',
                                                                name,
                                                                start_date,
                                                                stop_date,
                                                                session)
            tests = test_run_aggregator.TestRunAggregator(tests).aggregate(
                datetime_resolution=datetime_resolution)
        return tests

    output = _query_test_runs_by_build_name(value, start_date, stop_date)
    return jsonify({'tests': output})


@app.route('/runs', methods=['GET'])
def get_runs():
    start_date = _parse_datetimes(flask.request.args.get('start_date', None))
    stop_date = _parse_datetimes(flask.request.args.get('stop_date', None))
    with session_scope() as session:
        db_runs = api.get_all_runs_by_date(start_date, stop_date, session)
        runs = [run.to_dict() for run in db_runs]
        return jsonify({'runs': runs})


def _calc_amount_of_successful_runs(runs):
    """
    If there were no failures while there's any passes, then the run succeeded.
    If there's no fails and no passes, then the run did not succeeded.
    """
    was_run_successful = lambda x: 1 if x['fail'] == 0 and x['pass'] > 0 else 0
    successful_runs = map(was_run_successful, runs)
    return sum(successful_runs)


def _calc_amount_of_failed_runs(runs):
    """
    If there were any failure, then the whole run failed.
    """
    return sum((1 for r in runs if r['fail'] > 0))


def _aggregate_runs(runs_by_time_delta):
    aggregated_runs = []
    for time in runs_by_time_delta:
        runs_by_job_name = runs_by_time_delta[time]
        job_data = []
        for job_name in runs_by_job_name:
            runs = runs_by_job_name[job_name]
            amount_of_success = _calc_amount_of_successful_runs(runs)
            amount_of_failures = _calc_amount_of_failed_runs(runs)
            avg_runtime = sum(map(itemgetter('run_time'), runs)) / len(runs)
            job_data.append({'fail': amount_of_failures,
                             'pass': amount_of_success,
                             'mean_run_time': avg_runtime,
                             'job_name': job_name})
        runs_by_time = dict(datetime=time)
        runs_by_time['job_data'] = sorted(job_data, key=itemgetter('job_name'))
        aggregated_runs.append(runs_by_time)
    aggregated_runs.sort(key=itemgetter('datetime'))
    return dict(timedelta=aggregated_runs)


@app.route('/runs/key/<path:run_metadata_key>/<path:value>', methods=['GET'])
def get_runs_by_run_metadata_key(run_metadata_key, value):
    run_metadata_key = parse.unquote(run_metadata_key)
    value = parse.unquote(value)
    start_date = _parse_datetimes(flask.request.args.get('start_date', None))
    stop_date = _parse_datetimes(flask.request.args.get('stop_date', None))
    datetime_resolution = flask.request.args.get('datetime_resolution', 'day')

    if datetime_resolution not in ['sec', 'min', 'hour', 'day']:
        message = ('Datetime resolution: %s, is not a valid'
                   ' choice' % datetime_resolution)
        status_code = 400
        return abort(make_response(message, status_code))

    with session_scope() as session:
        runs = (api.get_time_series_runs_by_key_value(run_metadata_key,
                                                      value,
                                                      start_date,
                                                      stop_date,
                                                      session))
        # prepare run_times to be consumed for producing 'numeric' data.
        run_times = {}
        for run_at, run_data in runs.items():
            for run in run_data:
                if run['fail'] > 0 or run['pass'] == 0:
                    continue
                build_name = run['metadata']['build_name']
                if run_at in run_times:
                    if build_name in run_times[run_at]:
                        run_times[run_at][build_name].append(run['run_time'])
                    else:
                        run_times[run_at][build_name] = [run['run_time']]
                else:
                    run_times[run_at] = {build_name: [run['run_time']]}
        # if there is more than one run with the same run_at time
        # and build_name just average the results.
        for run_at, run_time_data in run_times.items():
            for build_name, times in run_time_data.items():
                run_times[run_at][build_name] = numpy.mean(times)
        numeric = run_aggregator.get_numeric_data(
            run_times, datetime_resolution)
        # Groups runs by metadata
        group_by = "build_name"
        runs_by_build_name = _group_runs_by_key(runs, group_by)
        # Group runs by the chosen data_range.
        # That does not apply when you choose 'sec' since runs are already
        # grouped by it.
        aggregated_runs = run_aggregator.RunAggregator(
            runs_by_build_name).aggregate(datetime_resolution)
        data = _aggregate_runs(aggregated_runs)
        return jsonify({'numeric': numeric, 'data': data})


@app.route('/runs/key/<path:run_metadata_key>/<path:value>/recent',
           methods=['GET'])
def get_recent_runs(run_metadata_key, value):
    run_metadata_key = parse.unquote(run_metadata_key)
    value = parse.unquote(value)
    runs = _get_recent_runs_data(run_metadata_key, value)
    return jsonify(runs)


@app.route('/runs/key/<path:run_metadata_key>/<path:value>/recent/detail',
           methods=['GET'])
def get_recent_runs_detail(run_metadata_key, value):
    run_metadata_key = parse.unquote(run_metadata_key)
    value = parse.unquote(value)
    runs = _get_recent_runs_data(run_metadata_key, value, detail=True)
    return jsonify(runs)


def _get_recent_runs_data(run_metadata_key, value, detail=False):
    num_runs = flask.request.args.get('num_runs', 10)
    with session_scope() as session:
        results = api.get_recent_runs_by_key_value_metadata(
            run_metadata_key, value, num_runs, session)
        runs = []
        for result in results:
            if detail:
                run = result.to_dict()
            else:
                if result.passes > 0 and result.fails == 0:
                    status = 'success'
                elif result.fails > 0:
                    status = 'fail'
                else:
                    continue

                run = {
                    'id': result.uuid,
                    'status': status,
                    'start_date': result.run_at.isoformat(),
                    'link': result.artifacts,
                }

            run_meta = api.get_run_metadata(result.uuid, session)
            for meta in run_meta:
                if meta.key == 'build_name':
                    run['build_name'] = meta.value
                    break
            runs.append(run)
    return runs


def _gen_feed(url, key, value):
    title = 'Failures for %s: %s' % (key, value)
    fg = feed.FeedGenerator()
    fg.title(title)
    fg.id(url)
    fg.link(href=url, rel='self')
    fg.description("The failed %s: %s tests feed" % (key, value))
    fg.language('en')
    return fg


@app.route('/runs/key/<path:run_metadata_key>/<path:value>/recent/rss',
           methods=['GET'])
def get_recent_failed_runs_rss(run_metadata_key, value):
    run_metadata_key = parse.unquote(run_metadata_key)
    value = parse.unquote(value)
    url = request.url
    if run_metadata_key not in feeds:
        feeds[run_metadata_key] = {value: _gen_feed(url,
                                                    run_metadata_key,
                                                    value)}
        feeds["last runs"][run_metadata_key] = {value: None}
    elif value not in feeds[run_metadata_key]:
        feeds[run_metadata_key][value] = _gen_feed(url,
                                                   run_metadata_key,
                                                   value)
        feeds["last runs"][run_metadata_key][value] = None
    fg = feeds[run_metadata_key][value]
    with session_scope() as session:
        failed_runs = api.get_recent_failed_runs_by_run_metadata(
            run_metadata_key, value,
            start_date=feeds["last runs"][run_metadata_key][value],
            session=session)
        if failed_runs:
            last_run = sorted([x.run_at for x in failed_runs])[-1]
            if feeds["last runs"][run_metadata_key][value] == last_run:
                return feeds[run_metadata_key][value].rss_str()
            feeds["last runs"][run_metadata_key][value] = last_run
        else:
            count = api.get_runs_counts_by_run_metadata(
                run_metadata_key, value, session=session)
            if count == 0:
                msg = 'No matching runs found with %s=%s' % (
                    run_metadata_key, value)
                return abort(make_response(msg, 404))
        for run in failed_runs:
            meta = api.get_run_metadata(run.uuid, session=session)
            failing_test_runs = api.get_failing_from_run(run.id,
                                                         session=session)
            uuid = [x.value for x in meta if x.key == 'build_uuid'][0]
            build_name = [x.value for x in meta if x.key == 'build_name'][0]
            entry = fg.add_entry()
            entry.id(uuid)
            entry.title('Failed Run %s/%s' % (build_name, uuid[:7]))
            entry.published(pytz.utc.localize(run.run_at))
            entry.link({'href': run.artifacts, 'rel': 'alternate'})
            metadata_url = rss_opts['frontend_url'] + '/#/' + parse.quote(
                'g/%s/%s' % (run_metadata_key, value))
            job_url = rss_opts['frontend_url'] + '/#/' + parse.quote(
                'job/%s' % build_name)
            content = '<ul>'
            content += '<li><a href="%s">Metadata page</a></li>\n' % (
                metadata_url)
            content += '<li><a href="%s">Job Page</a></li>' % (job_url)
            content += '</ul>'
            content += '<h3>Failed tests</h3>'
            content += '<ul>'
            for failing_test_run in failing_test_runs:
                content += '<li><a href="%s">%s</a></li>' % (
                    rss_opts['frontend_url'] + '/#/test/' +
                    failing_test_run.test.test_id,
                    failing_test_run.test.test_id)
            content += '</ul>'
            entry.description(content)
    response = make_response(feeds[run_metadata_key][value].rss_str())
    response.headers['Content-Type'] = 'application/xml; charset=utf-8'
    return response


@app.route('/tests/recent/<string:status>', methods=['GET'])
def get_recent_test_status(status):
    global region
    if not region:
        setup()
    status = parse.unquote(status)
    num_runs = flask.request.args.get('num_runs', 10)
    bug_dict = {}
    query_threads = []

    def _populate_bug_dict(change_num, patch_num, short_uuid, run):
        bug_dict[run] = elastic_recheck_cached(change_num, patch_num,
                                               short_uuid)

    @region.cache_on_arguments()
    def _get_recent(status):
        with session_scope() as session:
            failed_runs = api.get_recent_failed_runs(num_runs, session)
            job_names = {}
            for run in failed_runs:
                metadata = api.get_run_metadata(run, session=session)
                short_uuid = None
                change_num = None
                patch_num = None
                for meta in metadata:
                    if meta.key == 'build_short_uuid':
                        short_uuid = meta.value
                    elif meta.key == 'build_change':
                        change_num = meta.value
                    elif meta.key == 'build_patchset':
                        patch_num = meta.value
                    elif meta.key == 'build_name':
                        job_names[run] = meta.value
                    global classifier
                    if classifier:
                        # NOTE(mtreinish): If the required metadata fields
                        # aren't present skip ES lookup
                        if not short_uuid or not change_num or not patch_num:
                            continue
                        query_thread = threading.Thread(
                            target=_populate_bug_dict, args=(change_num,
                                                             patch_num,
                                                             short_uuid, run))
                        query_threads.append(query_thread)
                        query_thread.start()
            test_runs = api.get_test_runs_by_status_for_run_ids(
                status, failed_runs, session=session, include_run_id=True)
            output = []
            for run in test_runs:
                run['start_time'] = run['start_time'].isoformat()
                run['stop_time'] = run['stop_time'].isoformat()
                run['job_name'] = job_names.get(run['uuid'])
                output.append(run)
            for thread in query_threads:
                thread.join()
            return {'test_runs': output, 'bugs': bug_dict}
    results = _get_recent(status)
    return jsonify(results)


def _periodic_refresh_cache(cache, status, creator, mutex):
    def runner():
        try:
            value = creator()
            cache.set(status, value)
        finally:
            mutex.release()
    thread = threading.Thread(target=runner)
    thread.start()


@app.route('/run/<string:run_id>/tests', methods=['GET'])
def get_tests_from_run(run_id):
    run_id = parse.unquote(run_id)
    with session_scope() as session:
        db_tests = api.get_tests_from_run_id(run_id, session)
        tests = [test.to_dict() for test in db_tests]
        return jsonify({'tests': tests})


@app.route('/run/<string:run_id>/test_runs', methods=['GET'])
def get_run_test_runs(run_id):
    run_id = parse.unquote(run_id)
    with session_scope() as session:
        db_test_runs = api.get_tests_run_dicts_from_run_id(run_id, session)
        return jsonify(db_test_runs)


@app.route('/tests', methods=['GET'])
def get_tests():
    with session_scope() as session:
        db_tests = api.get_all_tests(session)
        tests = [test.to_dict() for test in db_tests]
        return jsonify({'tests': tests})


@app.route('/tests/prefix', methods=['GET'])
def get_test_prefixes():
    with session_scope() as session:
        return jsonify(api.get_test_prefixes(session))


@app.route('/tests/prefix/<path:prefix>', methods=['GET'])
def get_tests_by_prefix(prefix):
    prefix = parse.unquote(prefix)
    limit = flask.request.args.get('limit', 100)
    offset = flask.request.args.get('offset', 0)

    with session_scope() as session:
        db_tests = api.get_tests_by_prefix(prefix, session,
                                           limit=limit, offset=offset)

        tests = [test.to_dict() for test in db_tests]
        return jsonify({'tests': tests})


def _check_db_availability():
    try:
        global engine
        result = engine.execute('SELECT now()').first()
        if result is None:
            return False
        return True
    except Exception:
        return False


def _check_er_availability():
    global es_url
    global query_dir
    if not classifier:
        if not er:
            health = 'NotInstalled'
        elif not es_url or not query_dir:
            health = 'NotConfigured'
    else:
        url = classifier.config.es_url
        es = pyelasticsearch.ElasticSearch(url)
        health = {'Configured': {'elastic-search': es.health()['status']}}
    return health


@app.route('/status', methods=['GET'])
def get_status():

    is_db_available = _check_db_availability()
    is_er_available = _check_er_availability()

    status = {'status': {
        'availability': {
            'database': is_db_available,
            'elastic-recheck': is_er_available,
        },
        'version': version.VersionInfo(
            'openstack_health').version_string_with_vcs()
    }}
    response = jsonify(status)

    if not is_db_available:
        response.status_code = 500

    return response


def parse_command_line_args():
    description = 'Starts the API service for openstack-health'
    parser = argparse.ArgumentParser(description=description)

    parser.add_argument('config_file', type=str, nargs='?',
                        default='/etc/openstack-health.conf',
                        help='the path for the config file to be read.')
    return parser.parse_args()


@app.route('/test_runs/<path:test_id>', methods=['GET'])
def get_test_runs_for_test(test_id):
    test_id = parse.unquote(test_id)
    start_date = _parse_datetimes(flask.request.args.get('start_date', None))
    stop_date = _parse_datetimes(flask.request.args.get('stop_date', None))
    datetime_resolution = flask.request.args.get('datetime_resolution', 'min')

    if datetime_resolution not in ['sec', 'min', 'hour', 'day']:
        message = ('Datetime resolution: %s, is not a valid'
                   ' choice' % datetime_resolution)
        status_code = 400
        return abort(make_response(message, status_code))

    bug_dict = {}
    query_threads = []

    def _populate_bug_dict(change_dict):
        for run in change_dict:
            change_num = change_dict[run]['change_num']
            patch_num = change_dict[run]['patch_num']
            short_uuid = change_dict[run]['short_uuid']
            result = elastic_recheck_cached(change_num, patch_num,
                                            short_uuid)
            bug_dict[run] = result

    @region.cache_on_arguments()
    def _get_data(test_id, start_date, stop_date):
        with session_scope() as session:
            db_test_runs = api.get_test_runs_by_test_test_id(
                test_id, session=session, start_date=start_date,
                stop_date=stop_date)
            if not db_test_runs:
                # NOTE(mtreinish) if no data is returned from the DB just
                # return an empty set response, the test_run_aggregator
                # function assumes data is present.
                return {'numeric': {}, 'data': {}, 'failed_runs': {}}
            test_runs =\
                test_run_aggregator.convert_test_runs_list_to_time_series_dict(
                    db_test_runs, datetime_resolution)
            failed_run_ids = [
                x.run_id for x in db_test_runs if x.status == 'fail']
            failed_runs = api.get_runs_by_ids(failed_run_ids, session=session)
            job_names = {}
            providers = {}
            failed_uuids = [x.uuid for x in failed_runs]
            split_uuids = []
            if len(failed_uuids) <= 10:
                split_uuids = [[x] for x in failed_uuids]
            else:
                for i in range(0, len(failed_uuids), 10):
                    end = i + 10
                    split_uuids.append(failed_uuids[i:end])
            for uuids in split_uuids:
                change_dict = {}
                for uuid in uuids:
                    metadata = api.get_run_metadata(uuid, session=session)
                    short_uuid = None
                    change_num = None
                    patch_num = None
                    for meta in metadata:
                        if meta.key == 'build_short_uuid':
                            short_uuid = meta.value
                        elif meta.key == 'build_change':
                            change_num = meta.value
                        elif meta.key == 'build_patchset':
                            patch_num = meta.value
                        elif meta.key == 'build_name':
                            job_names[uuid] = meta.value
                        elif meta.key == 'node_provider':
                            providers[uuid] = meta.value
                    # NOTE(mtreinish): If the required metadata fields
                    # aren't present skip ES lookup
                    if not short_uuid or not change_num or not patch_num:
                        continue
                global classifier
                if classifier:
                    change_dict[uuid] = {
                        'change_num': change_num,
                        'patch_num': patch_num,
                        'short_uuid': short_uuid,
                    }
                    query_thread = threading.Thread(
                        target=_populate_bug_dict, args=[change_dict])
                    query_threads.append(query_thread)
                    query_thread.start()
            output = []
            for thread in query_threads:
                thread.join()
            for run in failed_runs:
                temp_run = {}
                temp_run['provider'] = providers.get(run.uuid)
                temp_run['job_name'] = job_names.get(run.uuid)
                temp_run['run_at'] = run.run_at.isoformat()
                temp_run['artifacts'] = run.artifacts
                temp_run['bugs'] = bug_dict.get(run.uuid, [])
                output.append(temp_run)
            test_runs['failed_runs'] = output
        return test_runs

    results = _get_data(test_id, start_date, stop_date)
    return jsonify(results)


def main():
    global config
    args = parse_command_line_args()
    config = ConfigParser.ConfigParser()
    config.read(args.config_file)
    try:
        host = config.get('default', 'host')
    except ConfigParser.NoOptionError:
        host = '127.0.0.1'
    try:
        port = config.getint('default', 'port')
    except ConfigParser.NoOptionError:
        port = 5000
    app.run(debug=False, host=host, port=port)


if __name__ == '__main__':
    main()
