import { useEffect, useRef, useState } from "react";
import { api, getCsrf } from "../api";
import type { Card } from "../types";

declare global {
  interface Window {
    YT?: any;
    onYouTubeIframeAPIReady?: () => void;
  }
}

let apiPromise: Promise<void> | null = null;

function loadIframeApi(): Promise<void> {
  if (window.YT?.Player) return Promise.resolve();
  if (!apiPromise) {
    apiPromise = new Promise((resolve) => {
      const prev = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => {
        prev?.();
        resolve();
      };
      const tag = document.createElement("script");
      tag.src = "https://www.youtube.com/iframe_api";
      document.head.appendChild(tag);
    });
  }
  return apiPromise;
}

interface PlayerProps {
  card: Card;
  onClose: () => void;
  /** error 100: video confirmed gone -> tombstone + replace (FLOW-20) */
  onUnavailable: (card: Card) => void;
}

/**
 * IFrame player with telemetry (FLOW-4): watched ranges tracked separately
 * from the playhead (1s polling; a seek >2s closes the current segment),
 * 15s heartbeats with a monotonic seq, sendBeacon on close/unload (AC-7 —
 * CSRF rides in the body because beacons cannot set headers; falls back to
 * keepalive fetch when the beacon is refused).
 *
 * Embed errors: 100 = deleted/private -> unavailable path; 101/150 = the
 * channel disabled embedding -> open-on-YouTube fallback, recorded as a
 * positive-lite click, NEVER a tombstone (§11/AC-38).
 */
export function Player({ card, onClose, onUnavailable }: PlayerProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [embedDisabled, setEmbedDisabled] = useState(false);
  const stateRef = useRef({
    sessionId: crypto.randomUUID(),
    seq: 0,
    ranges: [] as [number, number][],
    segStart: null as number | null,
    lastTime: 0,
    openedAt: Date.now(),
    closed: false,
    started: false,
    suppress: false,
  });

  const recordOpenOnYouTube = () => {
    void api("/interactions", {
      method: "POST",
      body: {
        video_id: card.video_id,
        selection_id: card.selection_id || null,
        kind: "open_on_youtube",
      },
    });
  };

  useEffect(() => {
    const s = stateRef.current;
    s.closed = false;
    // per-RUN cancellation: StrictMode runs effect->cleanup->effect
    // synchronously, and the shared flag alone would let the FIRST run's
    // async loadIframeApi callback proceed after its cleanup already ran
    // (leaking a duplicate player + unclearable timers)
    let cancelled = false;
    let player: any = null;
    let pollTimer: number | undefined;
    let beatTimer: number | undefined;

    const closedRanges = (): [number, number][] => {
      const out = [...s.ranges];
      if (s.segStart !== null && s.lastTime > s.segStart) {
        out.push([s.segStart, s.lastTime]);
      }
      return out;
    };

    const payload = (event: string) => ({
      watch_session_id: s.sessionId,
      video_id: card.video_id,
      selection_id: card.selection_id || null,
      seq: ++s.seq,
      position: s.lastTime,
      ranges: closedRanges(),
      exposure_seconds: (Date.now() - s.openedAt) / 1000,
      event,
      csrf: getCsrf(),
    });

    const send = (event: string, useBeacon = false) => {
      if (s.suppress) return;
      const body = JSON.stringify(payload(event));
      let queued = false;
      if (useBeacon && navigator.sendBeacon) {
        queued = navigator.sendBeacon(
          "/api/telemetry/watch",
          new Blob([body], { type: "application/json" }),
        );
      }
      if (!queued) {
        fetch("/api/telemetry/watch", {
          method: "POST",
          credentials: "include",
          keepalive: useBeacon,
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": getCsrf() ?? "",
          },
          body,
        }).catch(() => undefined);
      }
      s.ranges = closedRanges();
      if (s.segStart !== null) s.segStart = s.lastTime;
    };

    const poll = () => {
      if (!player?.getCurrentTime) return;
      const t = player.getCurrentTime();
      const playing = player.getPlayerState?.() === window.YT?.PlayerState?.PLAYING;
      if (playing) {
        if (s.segStart === null) {
          s.segStart = t;
        } else if (Math.abs(t - s.lastTime) > 2.5) {
          if (s.lastTime > s.segStart) s.ranges.push([s.segStart, s.lastTime]);
          s.segStart = t;
        }
        s.lastTime = t;
      } else if (s.segStart !== null) {
        if (s.lastTime > s.segStart) s.ranges.push([s.segStart, s.lastTime]);
        s.segStart = null;
      }
    };

    const onUnload = () => {
      if (!s.closed && s.started) send("close", true);
    };

    loadIframeApi().then(() => {
      if (cancelled || !hostRef.current) return;
      player = new window.YT.Player(hostRef.current, {
        videoId: card.video_id,
        playerVars: { autoplay: 1, rel: 0 },
        events: {
          onReady: () => {
            s.started = true;
          },
          onStateChange: (e: any) => {
            if (e.data === window.YT.PlayerState.ENDED) send("ended");
          },
          onError: (e: any) => {
            if (e.data === 100) {
              s.suppress = true;              // no junk close for a dead video
              onUnavailable(card);            // server verifies, then FLOW-20
            } else if (e.data === 101 || e.data === 150) {
              // fallback, no tombstone — and stop telemetry: nothing was
              // watchable, so heartbeats/close would fabricate an abandon
              s.suppress = true;
              if (pollTimer) clearInterval(pollTimer);
              if (beatTimer) clearInterval(beatTimer);
              setEmbedDisabled(true);
            }
          },
        },
      });
      pollTimer = window.setInterval(poll, 1000);
      beatTimer = window.setInterval(() => send("heartbeat"), 15000);
    });
    window.addEventListener("pagehide", onUnload);

    return () => {
      cancelled = true;
      s.closed = true;
      window.removeEventListener("pagehide", onUnload);
      if (pollTimer) clearInterval(pollTimer);
      if (beatTimer) clearInterval(beatTimer);
      poll();
      // only close out sessions that actually started (StrictMode's probe
      // mount/unmount must not emit junk telemetry)
      if (s.started || s.seq > 0) send("close", true);
      player?.destroy?.();
    };
  }, [card.video_id]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="player-overlay" onClick={onClose}>
      <div className="player-box" onClick={(e) => e.stopPropagation()}>
        {embedDisabled ? (
          <div className="frame" style={{ display: "flex", alignItems: "center",
                                          justifyContent: "center" }}>
            <div style={{ textAlign: "center", padding: 24 }}>
              <p>This channel disables embedded playback.</p>
              <a
                href={`https://www.youtube.com/watch?v=${card.video_id}`}
                target="_blank"
                rel="noreferrer"
                onClick={recordOpenOnYouTube}
              >
                <button className="primary">Open on YouTube</button>
              </a>
            </div>
          </div>
        ) : (
          <div className="frame">
            <div ref={hostRef} style={{ width: "100%", height: "100%" }} />
          </div>
        )}
        <div className="bar">
          <span className="title">{card.title}</span>
          <a
            href={`https://www.youtube.com/watch?v=${card.video_id}`}
            target="_blank"
            rel="noreferrer"
            onClick={recordOpenOnYouTube}
          >
            Open on YouTube
          </a>
          <button onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
