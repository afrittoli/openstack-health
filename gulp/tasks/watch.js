'use strict';

var config        = require('../config');
var gulp          = require('gulp');

gulp.task('watch', ['browserSync', 'server'], function() {

  // Scripts are automatically watched and rebundled by Watchify inside Browserify task
  gulp.watch(config.styles.src,       ['styles']);
  gulp.watch(config.fonts.src,        ['fonts']);
  gulp.watch(config.images.src,       ['images']);
  gulp.watch(config.data.src,         ['data']);
  gulp.watch(config.devResources.src, ['dev-resources']);
  gulp.watch(config.views.watch,      ['views']);

});
