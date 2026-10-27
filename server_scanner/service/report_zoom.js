(function (root) {
  'use strict';
  class ReportZoom {
    constructor(viewport, image, changed = () => {}) {
      this.viewport = viewport;
      this.image = image;
      this.changed = changed;
      this.scale = 1;
      this.x = this.y = 0;
      this.points = new Map();
      this.lastTap = null;
      this.multi = false;
      for (const type of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel']) {
        viewport.addEventListener(type, event => this.pointer(event));
      }
      viewport.addEventListener('dblclick', event => {
        event.preventDefault();
        if (this.lastPointerType !== 'touch') this.toggle(this.point(event));
      });
      viewport.addEventListener('wheel', event => {
        event.preventDefault();
        this.zoom(this.scale * Math.exp(-event.deltaY * 0.002), this.point(event));
      }, {passive: false});
      image.addEventListener('load', () => this.fit());
      root.addEventListener?.('resize', () => this.fit());
    }
    point(event) {
      const box = this.viewport.getBoundingClientRect();
      return {x: event.clientX - box.left - box.width / 2, y: event.clientY - box.top - box.height / 2};
    }
    fit() {
      const box = this.viewport.getBoundingClientRect();
      if (!box.width || !box.height) return;
      this.width = box.width;
      this.height = box.height;
      const ratio = Math.min(box.width / (this.image.naturalWidth || 1600), box.height / (this.image.naturalHeight || 900));
      this.imageWidth = (this.image.naturalWidth || 1600) * ratio;
      this.imageHeight = (this.image.naturalHeight || 900) * ratio;
      this.image.style.width = this.imageWidth + 'px';
      this.image.style.height = this.imageHeight + 'px';
      this.reset();
    }
    reset() {
      this.scale = 1;
      this.x = this.y = 0;
      this.points.clear();
      this.gesture = this.lastTap = null;
      this.multi = false;
      this.paint();
    }
    paint() {
      this.scale = Math.max(1, Math.min(8, this.scale));
      const maxX = Math.max(0, ((this.imageWidth || 0) * this.scale - (this.width || 0)) / 2);
      const maxY = Math.max(0, ((this.imageHeight || 0) * this.scale - (this.height || 0)) / 2);
      this.x = Math.max(-maxX, Math.min(maxX, this.x));
      this.y = Math.max(-maxY, Math.min(maxY, this.y));
      this.image.style.transform = `translate(-50%,-50%) translate(${this.x}px,${this.y}px) scale(${this.scale})`;
      this.changed(this.scale);
    }
    zoom(value, anchor = {x: 0, y: 0}) {
      const next = Math.max(1, Math.min(8, value)), ratio = next / this.scale;
      // 缩放围绕手指中点进行，避免被查看的 K 线从手指下跳走。
      this.x = anchor.x - (anchor.x - this.x) * ratio;
      this.y = anchor.y - (anchor.y - this.y) * ratio;
      this.scale = next;
      this.paint();
    }
    toggle(anchor) { this.zoom(this.scale > 1.05 ? 1 : 2.5, anchor); }
    begin() {
      const points = [...this.points.values()];
      if (points.length >= 2) {
        const [a, b] = points;
        this.gesture = {kind: 'pinch', distance: Math.max(1, Math.hypot(a.x - b.x, a.y - b.y)),
          center: {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2}, scale: this.scale, x: this.x, y: this.y};
        this.multi = true;
        this.lastTap = null;
      } else if (points.length === 1) {
        this.gesture = {kind: 'pan', start: points[0], x: this.x, y: this.y};
      } else this.gesture = null;
    }
    pointer(event) {
      if (event.type === 'pointerdown') {
        if (event.button != null && event.button !== 0) return;
        event.preventDefault();
        this.lastPointerType = event.pointerType;
        const point = this.point(event);
        if (!this.points.size) { this.multi = false; this.tap = {point, moved: false}; }
        this.points.set(event.pointerId, point);
        this.viewport.setPointerCapture?.(event.pointerId);
        this.begin();
        return;
      }
      if (!this.points.has(event.pointerId)) return;
      event.preventDefault();
      const point = this.point(event);
      if (event.type === 'pointermove') {
        this.points.set(event.pointerId, point);
        if (this.tap && Math.hypot(point.x - this.tap.point.x, point.y - this.tap.point.y) > 8) this.tap.moved = true;
        const g = this.gesture;
        if (this.points.size >= 2 && g?.kind === 'pinch') {
          const [a, b] = [...this.points.values()];
          const next = Math.max(1, Math.min(8, g.scale * Math.hypot(a.x - b.x, a.y - b.y) / g.distance));
          const center = {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2};
          this.x = center.x - (g.center.x - g.x) * next / g.scale;
          this.y = center.y - (g.center.y - g.y) * next / g.scale;
          this.scale = next;
        } else if (g?.kind === 'pan') {
          this.x = g.x + point.x - g.start.x;
          this.y = g.y + point.y - g.start.y;
        }
        this.paint();
        return;
      }
      if (event.type === 'pointerup' && event.pointerType === 'touch' && !this.multi && !this.tap?.moved) {
        const time = event.timeStamp ?? Date.now();
        if (this.lastTap && time - this.lastTap.time < 320 && Math.hypot(point.x - this.lastTap.x, point.y - this.lastTap.y) < 24) {
          this.toggle(point);
          this.lastTap = null;
        } else this.lastTap = {...point, time};
      }
      this.points.delete(event.pointerId);
      // 一指离开后重新记录拖动起点，双指缩放转单指拖动时不跳位。
      this.begin();
    }
  }
  root.ReportZoom = ReportZoom;
  if (typeof module === 'object' && module.exports) module.exports = ReportZoom;
})(globalThis);
