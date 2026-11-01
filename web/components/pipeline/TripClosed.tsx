"use client";
// web/components/pipeline/TripClosed.tsx — one "closed" state for every card of the trip page (AA-957 S262 round 3,
// design v3 C12 §2.7). The server render says whether the trip is cancelled or its cancellation waits for AA; the
// cancel dialog's run marks it closed at once, so the Proposal, Invoices, Bookings, Documents and Messages cards stop
// offering billing and planning in the same frame as the header (before, only the header read the run's answer and
// the cards offered "New invoice" / "Mark paid" for the ~2 s the server refresh took). Closed never reopens: a
// cancellation is one row per trip, never removed. S262 round 3 (video finding): between the run's answer and the
// server refresh (~0.4 s) the cards still held the server's old money (an issued invoice the run voids, a "Due"
// balance row, "Commission: Expected"): "updating" is that window, and the cards show it read-only (no Void, Record
// payment, refund step) with a short "updating" note, until the server view says what the run changed.
import { createContext, useContext, useMemo, useState } from "react";

type TripClosedState = { closed: boolean; updating: boolean; markClosed: () => void };

const TripClosedContext = createContext<TripClosedState>({ closed: false, updating: false, markClosed: () => {} });

export function TripClosedProvider({ closed, children }: { closed: boolean; children?: React.ReactNode }) {
  const [ran, setRan] = useState(false);
  // The run closed the trip but the server view (the ``closed`` prop) has not caught up: the cards' money is stale.
  const value = useMemo(() => ({ closed: closed || ran, updating: ran && !closed, markClosed: () => setRan(true) }), [closed, ran]);
  return <TripClosedContext.Provider value={value}>{children}</TripClosedContext.Provider>;
}

/** Whether the trip is closed: the page's provider, or the card's own prop (cards rendered outside the page). */
export function useTripClosed(own = false): boolean {
  return useContext(TripClosedContext).closed || own;
}

/** True between the cancel run's answer and the server view that reflects it: show the money read-only. */
export function useTripUpdating(): boolean {
  return useContext(TripClosedContext).updating;
}

/** Called by the cancel dialog's run: every card of the page reads the trip as closed from the next render. */
export function useMarkTripClosed(): () => void {
  return useContext(TripClosedContext).markClosed;
}
