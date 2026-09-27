// Emergency isolated-review layout seam for the staging.8 parent C-grid.
const ELEMENT = "mtae-wall10-live-r4-card";
const PATCH_FLAG = Symbol.for("mtae.wall10.cgrid.layout.v1");

function installPatch() {
  const Card = customElements.get(ELEMENT);
  if (!Card || Card.prototype[PATCH_FLAG]) return Boolean(Card);
  const originalRender = Card.prototype._render;
  if (typeof originalRender !== "function") throw new Error(`${ELEMENT} has no _render method`);
  Card.prototype._render = function (...args) {
    const result = originalRender.apply(this, args);
    if (this._config?.site?.home_c_grid && this.shadowRoot) {
      const style = document.createElement("style");
      style.dataset.mtaeCGridLayoutV1 = "";
      style.textContent = `
        .home-c-grid-host {
          grid-column: 1 / span 2 !important;
          grid-row: 1 / span 2 !important;
          width: 100% !important;
          height: 100% !important;
          min-width: 0 !important;
          min-height: 0 !important;
        }
        .home-c-grid-host > van-gogh2-home-c-grid-card {
          display: block !important;
          width: 100% !important;
          height: 100% !important;
          min-width: 0 !important;
          min-height: 0 !important;
        }
      `;
      this.shadowRoot.append(style);
    }
    return result;
  };
  Card.prototype[PATCH_FLAG] = true;
  return true;
}

if (!installPatch()) customElements.whenDefined(ELEMENT).then(installPatch);
