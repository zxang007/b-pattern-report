'use strict';
const assert = require('node:assert/strict');
const ReportZoom = require('./report_zoom.js');
function fixture() {
  const handlers = {};
  const viewport = {getBoundingClientRect: () => ({left: 0, top: 0, width: 400, height: 300}),
    addEventListener: (type, callback) => {handlers[type] = callback;}, setPointerCapture() {}};
  const image = {style: {}, naturalWidth: 1600, naturalHeight: 900, addEventListener() {}};
  const zoom = new ReportZoom(viewport, image);
  zoom.fit();
  const pointer = (type, id, x, y, time = 0) => zoom.pointer({type, pointerId: id, pointerType: 'touch',
    clientX: x, clientY: y, timeStamp: time, button: 0, preventDefault() {}});
  return {zoom, image, handlers, pointer};
}
{
  const {zoom, image, pointer} = fixture();
  assert.equal(image.style.width, '400px');
  pointer('pointerdown', 1, 100, 150);
  pointer('pointerdown', 2, 300, 150);
  pointer('pointermove', 1, 50, 150);
  pointer('pointermove', 2, 350, 150);
  assert.equal(zoom.scale, 1.5);
  assert.equal(zoom.x, 0);
  pointer('pointerup', 2, 350, 150);
  assert.equal(zoom.x, 0);
  pointer('pointermove', 1, 100, 150);
  assert.equal(zoom.x, 50);
  pointer('pointermove', 1, 1000, 150);
  assert.equal(zoom.x, 100);
  zoom.reset();
  assert.equal(zoom.scale, 1);
  assert.equal(zoom.x, 0);
}
{
  const {zoom, pointer} = fixture();
  pointer('pointerdown', 1, 100, 150);
  pointer('pointerdown', 2, 300, 150);
  pointer('pointermove', 1, -5000, 150);
  assert.equal(zoom.scale, 8);
  pointer('pointermove', 1, 299, 150);
  assert.equal(zoom.scale, 1);
  pointer('pointercancel', 1, 299, 150);
  pointer('pointercancel', 2, 300, 150);
  assert.equal(zoom.points.size, 0);
}
{
  const {zoom, pointer} = fixture();
  pointer('pointerdown', 1, 200, 150, 1000);
  pointer('pointerup', 1, 200, 150, 1050);
  pointer('pointerdown', 2, 200, 150, 1200);
  pointer('pointerup', 2, 200, 150, 1250);
  assert.equal(zoom.scale, 2.5);
  pointer('pointerdown', 3, 200, 150, 1400);
  pointer('pointerup', 3, 200, 150, 1450);
  pointer('pointerdown', 4, 200, 150, 1550);
  pointer('pointerup', 4, 200, 150, 1600);
  assert.equal(zoom.scale, 1);
}
{
  const {zoom, handlers} = fixture();
  zoom.zoom(2, {x: 50, y: 0});
  assert.equal(zoom.x, -50);
  const world = (50 - zoom.x) / zoom.scale;
  zoom.zoom(3, {x: 50, y: 0});
  assert.equal((50 - zoom.x) / zoom.scale, world);
  handlers.dblclick({clientX: 200, clientY: 150, preventDefault() {}});
  assert.equal(zoom.scale, 1);
}
process.stdout.write('手势检查通过：双指缩放、单指拖动、边界限制、双击还原、缩放锚点。\n');
