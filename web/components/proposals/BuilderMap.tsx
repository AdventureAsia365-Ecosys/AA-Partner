"use client";
// web/components/proposals/BuilderMap.tsx — the proposal builder's map (AA-948, card CRM-20). It draws the trip days
// that have a place (the version's served whole AA tours, `map_points` of the builder view) with the shared map engine
// (lib/map-engine, a byte copy of AA-TripPlanner-Web's: never TripPlanner app code, CI import guard): numbered day
// stops joined in day order, the agency's accent as the route colour. A click on a stop selects that day in the
// builder and names its places. The days without a place are listed under the map, never pinned at a guess.
// Empty states, never an error: no token configured (the map service is not set up), or no day with a place yet
// (no map is loaded then, so an empty builder costs no map load). The map sits inside the builder page, so it follows
// the builder's own access (CRM add-on, member role); it adds no feature gate of its own.
import "mapbox-gl/dist/mapbox-gl.css";
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { EmptyState, K } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { POPUP_TEXT, builderMapColors, builderMapPlan, type BuilderMapPlan, type MapDay } from "@/lib/builderMap";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { addRouteLayers, routeCollection, routeLayerIds, type LngLat, type MapboxModule } from "@/lib/map-engine";
import { useMapEngine } from "@/lib/map-engine/react";

const SOURCE = "builder-route";
const PREFIX = "builder-route";
const STOP_RADIUS = 11;
const ACTIVE_RADIUS = 14;

export function BuilderMap({
  days,
  token,
  lang,
  activeDayId,
  onDay,
  load,
}: {
  days: readonly MapDay[];
  /** The public Mapbox token from the server's runtime config (lib/mapConfig.ts); empty = not set up. */
  token?: string;
  lang: Lang;
  activeDayId?: string | null;
  onDay: (dayId: string) => void;
  /** Test seam: how to get the mapbox-gl module (default: the engine's dynamic import). */
  load?: () => Promise<MapboxModule>;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const plan = useMemo(() => builderMapPlan(days), [days]);
  const empty = !token
    ? { title: tr("builderMap.noToken.title"), body: tr("builderMap.noToken.body"), testId: "builder-map-no-token" }
    : plan.stops.length === 0
      ? { title: tr("builderMap.empty.title"), body: tr("builderMap.empty.body"), testId: "builder-map-empty" }
      : null;
  const activeLabel = Object.entries(plan.dayOf).find(([, id]) => id === activeDayId)?.[0] ?? null;
  return (
    <Panel title={tr("builderMap.title")} testId="builder-map">
      {empty ? (
        <div data-testid={empty.testId}>
          <EmptyState title={empty.title} description={empty.body} />
        </div>
      ) : (
        <BuilderMapCanvas plan={plan} token={token ?? ""} lang={lang} activeLabel={activeLabel} onDay={onDay} load={load} />
      )}
      {token && plan.stops.length > 0 && plan.unplaced.length > 0 && (
        <p data-testid="builder-map-unplaced" style={{ margin: "8px 0 0", fontSize: 12.5, color: K.muted }}>
          {fmt(tr("builderMap.unplaced"), { days: plan.unplaced.join(", ") })}
        </p>
      )}
    </Panel>
  );
}

/** The popup text of a day's stop: "Day 2: Mosswater (approximate area), Owl Ridge". */
export function stopText(plan: BuilderMapPlan, label: string, lang: Lang): string {
  const approx = t(lang, "builderMap.approximate");
  const places = (plan.names[label] ?? []).map((p) => (p.approximate ? `${p.name} (${approx})` : p.name)).join(", ");
  return fmt(t(lang, "builderMap.stop"), { n: label, places });
}

function BuilderMapCanvas({
  plan,
  token,
  lang,
  activeLabel,
  onDay,
  load,
}: {
  plan: BuilderMapPlan;
  token: string;
  lang: Lang;
  activeLabel: string | null;
  onDay: (dayId: string) => void;
  load?: () => Promise<MapboxModule>;
}) {
  // The click handler is bound once per map: it reads the latest plan, language and callback through this ref.
  const latest = useRef({ plan, lang, onDay });
  useLayoutEffect(() => {
    latest.current = { plan, lang, onDay };
  });
  const { containerRef, engine, ready, failed } = useMapEngine({
    accessToken: token,
    load,
    onReady: (e) => {
      const read = (v: string) => getComputedStyle(e.map.getContainer()).getPropertyValue(v);
      const colors = builderMapColors(read);
      addRouteLayers(e.map, SOURCE, PREFIX, {
        lineColor: colors.line,
        stopColor: colors.stop,
        labelColor: colors.label,
        dashed: false,
        stopRadius: STOP_RADIUS,
        labelSize: 11,
      });
      e.onLayerClick([routeLayerIds(PREFIX).stops], (props, coord) => {
        const label = String(props.label ?? "");
        const now = latest.current;
        const dayId = now.plan.dayOf[label];
        if (dayId) now.onDay(dayId);
        if (coord) {
          const box = document.createElement("div");
          box.textContent = stopText(now.plan, label, now.lang);
          box.style.fontSize = "13px";
          box.style.color = POPUP_TEXT; // the Mapbox popup is white in both themes
          e.showPopup(coord as LngLat, box);
        }
      });
    },
  });

  useEffect(() => {
    if (!engine || !ready) return;
    engine.setData(SOURCE, routeCollection(plan.stops, 8));
    engine.fitTo(plan.frame, { padding: 48, maxZoom: 9 });
  }, [engine, ready, plan]);

  useEffect(() => {
    if (!engine || !ready) return;
    engine.map.setPaintProperty(routeLayerIds(PREFIX).stops, "circle-radius", [
      "case",
      ["==", ["get", "label"], activeLabel ?? ""],
      ACTIVE_RADIUS,
      STOP_RADIUS,
    ]);
  }, [engine, ready, activeLabel]);

  if (failed) {
    return (
      <p data-testid="builder-map-failed" style={{ margin: 0, fontSize: 13, color: K.muted }}>
        {t(lang, "builderMap.failed")}
      </p>
    );
  }
  return (
    <div
      role="region"
      aria-label={t(lang, "builderMap.label")}
      data-testid="builder-map-canvas"
      style={{ position: "relative", height: "clamp(220px, 38vh, 360px)", borderRadius: 8, overflow: "hidden", border: `1px solid ${K.line}` }}
    >
      <div ref={containerRef} style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }} />
    </div>
  );
}
