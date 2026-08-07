import { useCallback, useEffect, useRef, useState } from "react";
import { api, getCsrf } from "../api";
import { Player } from "../components/Player";
import { VideoCard } from "../components/VideoCard";
import { confirmModal } from "../components/Modal";
import type { AppStatus, Card, Edition } from "../types";

// module-scoped so SPA-internal navigation (unmount) can't strand a failed
// flush — the next mount's interval retries whatever is still pending
const modulePending = new Set<number>();
const moduleSent = new Set<number>();

export function EditionPage() {
  const [edition, setEdition] = useState<Edition | null>(null);
  const [status, setStatus] = useState<AppStatus | null>(null);
  const [quotaLine, setQuotaLine] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState<Card | null>(null);
  const pendingImpressions = useRef<Set<number>>(modulePending);
  const sentImpressions = useRef<Set<number>>(moduleSent);

  const reload = useCallback(async () => {
    try {
      setError(null);
      const [ed, st, q] = await Promise.all([
        api<Edition>("/edition"),
        api<AppStatus>("/status"),
        api<{ total: { charged: number; budget: number } }>("/quota"),
      ]);
      setEdition(ed);
      setStatus(st);
      // AC-10: quota is visible, so "regenerate is free" is checkable
      setQuotaLine(`API units today: ${q.total.charged}/${q.total.budget}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      try {
        setStatus(await api<AppStatus>("/status"));
      } catch {
        /* unauthenticated handled upstream */
      }
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  // batch impression sends every 3s; ids stay pending until the POST
  // SUCCEEDS (a failure must not silently erase a real impression), and a
  // pagehide flushes the rest via sendBeacon (csrf in body)
  useEffect(() => {
    const flushBeacon = () => {
      const batch = [...pendingImpressions.current];
      if (!batch.length || !navigator.sendBeacon) return;
      const body = JSON.stringify({
        selection_ids: batch,
        csrf: getCsrf(),
      });
      if (
        navigator.sendBeacon(
          "/api/telemetry/impressions",
          new Blob([body], { type: "application/json" }),
        )
      ) {
        pendingImpressions.current.clear();
      }
    };
    const timer = setInterval(async () => {
      const batch = [...pendingImpressions.current];
      if (!batch.length) return;
      try {
        await api("/telemetry/impressions", {
          method: "POST",
          body: { selection_ids: batch },
        });
        for (const id of batch) pendingImpressions.current.delete(id);
      } catch {
        /* keep pending; retried next tick */
      }
    }, 3000);
    window.addEventListener("pagehide", flushBeacon);
    return () => {
      clearInterval(timer);
      window.removeEventListener("pagehide", flushBeacon);
      // SPA-internal navigation unmounts this page — flush what's buffered
      // (ids stay in the set until success; a duplicate later send is
      // harmless — the server upserts ON CONFLICT DO NOTHING)
      const batch = [...pendingImpressions.current];
      if (batch.length) {
        void api("/telemetry/impressions", {
          method: "POST",
          body: { selection_ids: batch },
        })
          .then(() => {
            // delete only THIS batch — a remount may have queued new ids
            // after the request left
            for (const id of batch) pendingImpressions.current.delete(id);
          })
          .catch(() => undefined);
      }
    };
  }, []);

  const onImpressed = useCallback((selectionId: number) => {
    if (sentImpressions.current.has(selectionId)) return;
    sentImpressions.current.add(selectionId);
    pendingImpressions.current.add(selectionId);
  }, []);

  const regenerate = async () => {
    const ok = await confirmModal({
      title: "Regenerate edition",
      message:
        "Compose a fresh edition from the already-scored pool. Free — no API quota is spent.",
      okLabel: "Regenerate",
    });
    if (!ok) return;
    try {
      await api("/edition/regenerate", { method: "POST" });
      sentImpressions.current.clear();
      pendingImpressions.current.clear();
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const goDeeper = async () => {
    // AC-11: cost shown BEFORE confirmation, exact spend after
    try {
      const preview = await api<{
        searches: number;
        estimated_units: number;
        category_used: number;
        category_cap: number;
        queries_available: number;
      }>("/edition/go-deeper/preview");
      if (!preview.queries_available) {
        setError("No search queries yet — run `mytube queries bootstrap` first.");
        return;
      }
      const ok = await confirmModal({
        title: "Go deeper",
        message:
          `Run ${preview.searches} fresh searches for ~${preview.estimated_units} ` +
          `API units (go-deeper used today: ${preview.category_used}/` +
          `${preview.category_cap}). New finds are hydrated now and scored ` +
          `within ~30 minutes.`,
        okLabel: `Spend ~${preview.estimated_units} units`,
      });
      if (!ok) return;
      const result = await api<{
        searches_executed: number;
        new_candidates: number;
        units_charged: number;
        stopped: string | null;
        hydration_stopped: string | null;
        quota_after: { charged: number; budget: number };
      }>("/edition/go-deeper", { method: "POST" });
      setQuotaLine(
        `API units today: ${result.quota_after.charged}/${result.quota_after.budget}`,
      );
      setError(null);
      const caveats = [
        result.stopped ? `search stopped early (${result.stopped})` : null,
        result.hydration_stopped
          ? `new finds not yet hydrated (${result.hydration_stopped}) — the background job will finish them`
          : null,
      ].filter(Boolean);
      setEdition((e) =>
        e
          ? {
              ...e,
              note:
                `Go deeper: ${result.searches_executed} searches, ` +
                `${result.new_candidates} new candidates, ` +
                `${result.units_charged} units charged. Regenerate after ` +
                `scoring (~30 min) to surface them.` +
                (caveats.length ? ` Note: ${caveats.join("; ")}.` : ""),
            }
          : e,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const onUnavailable = async (card: Card) => {
    setPlaying(null);
    await api(`/selections/${card.selection_id}/unavailable`, { method: "POST" });
    await reload();
  };

  return (
    <div className="page">
      {status && !status.ready && (
        <div className="banner">
          Getting ready: {status.subscriptions} subscriptions imported,{" "}
          {status.hydrated_videos} videos hydrated, {status.scored_videos} scored.
          The first edition can compose once subscriptions are in and the first
          hydration cycle has finished.
        </div>
      )}
      {edition?.note && <div className="banner info">{edition.note}</div>}
      {error && <div className="banner">{error}</div>}
      <div className="edition-head">
        <h1>Today&apos;s edition</h1>
        {edition && (
          <span className="muted">
            {edition.local_date} · generation {edition.generation_no} ·{" "}
            {edition.cards.length} cards
          </span>
        )}
        <span style={{ flex: 1 }} />
        {quotaLine && <span className="muted">{quotaLine}</span>}
        <button onClick={regenerate}>Regenerate (free)</button>
        <button onClick={goDeeper}>Go deeper</button>
      </div>
      {edition && (
        <div className="cards">
          {edition.cards.map((card) => (
            <VideoCard
              key={card.selection_id}
              card={card}
              onPlay={setPlaying}
              onImpressed={onImpressed}
              onChanged={reload}
            />
          ))}
        </div>
      )}
      {!edition && !error && <p className="center-note">Loading…</p>}
      {playing && (
        <Player
          card={playing}
          onClose={() => {
            setPlaying(null);
            void reload();
          }}
          onUnavailable={onUnavailable}
        />
      )}
    </div>
  );
}
