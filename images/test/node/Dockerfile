FROM n42org/tox

# Build dependencies
RUN apt-get update && apt-get install -q -y \
    git curl build-essential libssl-dev

RUN curl -sLO https://deb.nodesource.com/setup_4.x \
    && bash setup_4.x \
    && apt-get -y install nodejs git chromium-browser

RUN npm install -g eslint@1.5.1 eslint-plugin-angular@0.12.0 eslint-config-openstack@1.2.2

# Cleanup
RUN apt-get remove -q -y build-essential libssl-dev rsync; \
    apt-get clean; apt-get purge -y --auto-remove
