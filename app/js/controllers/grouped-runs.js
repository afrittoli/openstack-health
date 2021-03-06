'use strict';

var controllersModule = require('./_index');

/**
 * @ngInject
 */
function GroupedRunsController(
    $scope, pageTitleService, healthService, viewService, periodsService,
    runMetadataKey, name, $location) {

  // ViewModel
  var vm = this;

  vm.searchJob = $location.search().searchJob || '';

  // decodeURI is needed here because project names contains slash as part
  // of the name. As this come from an URL part and URL can be encoded,
  // this decode call make the project name exhibition properly.
  vm.runMetadataKey = decodeURIComponent(runMetadataKey);
  vm.name = decodeURIComponent(name);
  vm.recentRuns = [];
  vm.loaded = false;
  vm.hold = 0;

  // update the global grouping key - if we arrived here directly, it will not
  // be set already
  viewService.groupKey(runMetadataKey);

  // Updates the page title based on the selected runMetadataKey
  pageTitleService.update(vm.runMetadataKey);

  var configurePeriods = function() {
    vm.hold += 1;

    var res = viewService.resolution();
    var periods = periodsService.get('grouped-runs', res.key);

    viewService.periods(periods.min, periods.max, true);
    viewService.preferredDuration(periods.preference);

    vm.hold -= 1;
  };

  vm.processData = function(data, regex) {
    // prepare chart data
    var jobs = {};
    var passEntries = [];
    var failEntries = [];
    var failRateEntries = [];
    var pattern = null;
    try {
      pattern = new RegExp(regex);
    } catch (e) {
      pattern = '';
    }

    if (!data.timedelta) {
      return;
    }

    data.timedelta.forEach(function(timedelta) {
      var totalPass = 0;
      var totalFail = 0;
      var failRate = 0;
      var DEFAULT_FAIL_RATE = 0;

      timedelta.job_data.forEach(function(job) {
        if (pattern.test(job.job_name)) {

          var successfulJobs = 0;
          var failedJobs = 0;
          var jobFailRate = 0;

          if (!jobs[job.job_name]) {
            var jobMetrics = {
              name: job.job_name,
              passes: 0,
              failures: 0,
              failuresRate: 0
            };
            jobs[job.job_name] = jobMetrics;
          }

          totalPass += job.pass;
          totalFail += job.fail;

          jobs[job.job_name].passes += job.pass;
          jobs[job.job_name].failures += job.fail;

          successfulJobs = jobs[job.job_name].passes;
          failedJobs = jobs[job.job_name].failures;
          jobFailRate = (failedJobs / (failedJobs + successfulJobs)) * 100 || DEFAULT_FAIL_RATE;

          jobs[job.job_name].failuresRate = jobFailRate;
        }
      });

      failRate = totalFail / (totalFail + totalPass) || DEFAULT_FAIL_RATE;

      passEntries.push({
        x: new Date(timedelta.datetime).getTime(),
        y: totalPass
      });

      failEntries.push({
        x: new Date(timedelta.datetime).getTime(),
        y: totalFail
      });

      failRateEntries.push({
        x: new Date(timedelta.datetime).getTime(),
        y: failRate * 100
      });
    });

    vm.passes = passEntries;
    vm.failures = failEntries;
    vm.failRates = failRateEntries;

    vm.chartDataRate = [
      { key: '% Failures', values: failRateEntries }
    ];

    vm.jobs = Object.keys(jobs).map(function(name) {
      return jobs[name];
    });
  };

  vm.loadData = function() {
    if (vm.hold > 0) {
      return;
    }

    healthService.getRunsForRunMetadataKey(vm.runMetadataKey, vm.name, {
      start_date: viewService.periodStart(),
      stop_date: viewService.periodEnd(),
      datetime_resolution: viewService.resolution().key
    }).then(function(response) {
      vm.processData(response.data.data, vm.searchJob);
      vm.loaded = true;
    });
    healthService.getRecentGroupedRuns(vm.runMetadataKey, vm.name).then(function(response) {
      vm.recentRuns = response.data;
    });
  };

  configurePeriods();
  vm.loadData();

  $scope.$on('view:resolution', function(event, resolution) {
    configurePeriods();
    vm.loadData();
  });

  $scope.$on('view:period', function(event, corrected) {
    if (vm.loaded && !corrected) {
      vm.loadData();
    }
  });

  vm.onSearchChange = function() {
    $location.search('searchJob', $scope.groupedRuns.searchJob).replace();
    vm.loadData();
  };
}

controllersModule.controller('GroupedRunsController', GroupedRunsController);
