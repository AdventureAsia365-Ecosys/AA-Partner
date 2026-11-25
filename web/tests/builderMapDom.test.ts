// @vitest-environment jsdom
// AA-948 (card CRM-20), in the browser: the proposal builder's map (components/proposals/BuilderMap.tsx). Without a
// token it shows the "map not set up" state (no error, no map load); with a token but no placed day it shows how to get
// one (still no map load); with placed days it draws them through the shared map engine (a fake mapbox-gl module here),
// lists the days without a place, and a click on a stop selects that day. Invented places only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BuilderMap } from "@/components/proposals/BuilderMap";
import type { MapDay } from "@/lib/builderMap";
import { MESSAGES } from "@/lib/i18n";
import type { MapboxModule } from "@/lib/map-engine";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
});

type Handler = (arg?: unknown) => void;
class FakeMap {
  static last: FakeMap | null = null;
  handlers: { ev: string; layer?: string; fn: Handler }[] = [];
  sources = new Map<string, { data: unknown; setData: (d: unknown) => void }>();
  layers: { id: string }[] = [];
  paint: Record<string, unknown> = {};
  fits = 0;
  removed = 0;
  container: HTMLElement;
  constructor(o: { container: HTMLElement }) {
    this.container = o.container;
    FakeMap.last = this;
  }
  on(ev: string, a: unknown, b?: unknown) {
    if (typeof a === "function") this.handlers.push({ ev, fn: a as Handler });
    else this.handlers.push({ ev, layer: a as string, fn: b as Handler });
  }
  fire(ev: string, layer?: string, arg?: unknown) {
    for (const h of this.handlers) if (h.ev === ev && h.layer === layer) h.fn(arg);
  }
  resize() {}
  remove() {
    this.removed++;
  }
  getContainer() {
    return this.container;
  }
  getCanvas() {
    return { style: { cursor: "" } };
  }
  getSource(id: string) {
    return this.sources.get(id);
  }
  addSource(id: string, spec: { data: unknown }) {
    const src = { data: spec.data, setData: (d: unknown) => (src.data = d) };
    this.sources.set(id, src);
  }
  addLayer(l: { id: string }) {
    this.layers.push(l);
  }
  setPaintProperty(layer: string, prop: string, value: unknown) {
    this.paint[`${layer}.${prop}`] = value;
  }
  fitBounds() {
    this.fits++;
  }
  easeTo() {}
}
class FakeMarker {
  setLngLat() {
    return this;
  }
  addTo() {
    return this;
  }
  remove() {}
}
const popups: string[] = [];
class FakePopup {
  el: HTMLElement | null = null;
  setLngLat() {
    return this;
  }
  setDOMContent(el: HTMLElement) {
    this.el = el;
    return this;
  }
  addTo() {
    popups.push(this.el?.textContent ?? "");
    return this;
  }
  remove() {}
}

function fakeLoad() {
  const gl = { accessToken: "", Map: FakeMap, Marker: FakeMarker, Popup: FakePopup };
  return vi.fn(async () => gl as unknown as MapboxModule);
}

const TOKEN = "pk.example-public.fake-part";
const DAYS: MapDay[] = [
  { proposal_day_id: "d0", day_no: 0, before_you_go: true, map_points: [] },
  { proposal_day_id: "d1", day_no: 1, before_you_go: false,
    map_points: [{ name: "Juniper Pass", lat: 27.47, lng: 89.64, approximate: false }] },
  { proposal_day_id: "d2", day_no: 2, before_you_go: false,
    map_points: [{ name: "Mosswater", lat: 27.52, lng: 89.71, approximate: true }] },
  { proposal_day_id: "d3", day_no: 3, before_you_go: false, map_points: [] },
]; // prettier-ignore

async function mount(props: Partial<Parameters<typeof BuilderMap>[0]>): Promise<HTMLElement> {
  const el = document.createElement("div");
  document.body.appendChild(el);
  await act(async () => {
    const root = createRoot(el);
    roots.push(root);
    root.render(
      createElement(BuilderMap, { days: DAYS, token: TOKEN, lang: "en", activeDayId: null, onDay: () => {}, ...props }),
    );
  });
  return el;
}

describe("the builder map", () => {
  it("without a token shows the 'not set up' state and loads no map", async () => {
    const load = fakeLoad();
    const el = await mount({ token: "", load });
    expect(el.querySelector('[data-testid="builder-map-no-token"]')?.textContent).toContain(MESSAGES.en["builderMap.noToken.title"]);
    expect(el.querySelector('[data-testid="builder-map-canvas"]')).toBeNull();
    expect(el.querySelector('[data-testid="builder-map-unplaced"]')).toBeNull();
    expect(load).not.toHaveBeenCalled();
  });

  it("with a token but no placed day says how to get one and loads no map", async () => {
    const load = fakeLoad();
    const el = await mount({ days: [DAYS[0], DAYS[3]], load, lang: "vi" });
    expect(el.querySelector('[data-testid="builder-map-empty"]')?.textContent).toContain(MESSAGES.vi["builderMap.empty.title"]);
    expect(load).not.toHaveBeenCalled();
  });

  it("draws the placed days, lists the others, selects a day from its stop and names its places", async () => {
    const load = fakeLoad();
    const onDay = vi.fn();
    const el = await mount({ load, onDay, activeDayId: "d2" });
    expect(load).toHaveBeenCalledTimes(1);
    expect(el.querySelector('[data-testid="builder-map-canvas"]')?.getAttribute("aria-label")).toBe(MESSAGES.en["builderMap.label"]);
    expect(el.querySelector('[data-testid="builder-map-unplaced"]')?.textContent).toBe("Days without a place on the map: 3");
    const map = FakeMap.last!;
    await act(async () => map.fire("load"));
    expect(map.layers.map((l) => l.id)).toEqual(["builder-route-line", "builder-route-stops", "builder-route-labels"]);
    const fc = map.sources.get("builder-route")!.data as { features: { geometry: { type: string }; properties: { label?: string } }[] };
    expect(fc.features.map((f) => f.geometry.type)).toEqual(["LineString", "Point", "Point"]);
    expect(fc.features.slice(1).map((f) => f.properties.label)).toEqual(["1", "2"]);
    expect(map.fits).toBe(1);
    expect(JSON.stringify(map.paint["builder-route-stops.circle-radius"])).toContain('"2"'); // the chosen day is larger
    await act(async () =>
      map.fire("click", "builder-route-stops", {
        features: [{ properties: { label: "2" }, geometry: { type: "Point", coordinates: [89.71, 27.52] } }],
      }),
    );
    expect(onDay).toHaveBeenCalledWith("d2");
    expect(popups.at(-1)).toBe("Day 2: Mosswater (approximate area)");
  });

  it("removes the map when the builder leaves the page", async () => {
    const el = await mount({ load: fakeLoad() });
    const map = FakeMap.last!;
    expect(el.querySelector('[data-testid="builder-map-canvas"]')).not.toBeNull();
    for (const r of roots.splice(0)) act(() => r.unmount());
    expect(map.removed).toBe(1);
  });
});
