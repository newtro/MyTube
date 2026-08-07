import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Card } from "../types";
import { confirmModal, ReasonModal } from "./Modal";

/** Compact publish age — freshness is a first-class signal for news/AI
 *  content, so every non-blind card states how old it is. */
function formatAge(published: string | null | undefined): string {
  if (!published) return "";
  const then = new Date(published).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.floor((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  if (days < 30) return `${Math.floor(days / 7)}w ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  const years = (days / 365).toFixed(days < 730 ? 1 : 0);
  return `${years}y ago`;
}

/** Old content is easy to miss in a wall of cards — flag it. */
function ageClass(published: string | null | undefined): string {
  if (!published) return "";
  const days = (Date.now() - new Date(published).getTime()) / 86400000;
  if (Number.isNaN(days)) return "";
  if (days >= 365) return " age-ancient";
  if (days >= 90) return " age-old";
  if (days <= 2) return " age-fresh";
  return "";
}

function formatDuration(seconds: number | null): string {
  if (!seconds) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

interface Receipt {
  slot_class: string;
  blind: boolean;
  explanation?: string;
  anchors?: { kind: string; id: string; title: string; sim: number }[];
  steering?: { directive_id: number; summary: string | null }[];
  overpromise?: {
    overpromise: number;
    watches: number;
    clickbait_count: number;
    penalty_applied: number;
  } | null;
}

interface VideoCardProps {
  card: Card;
  onPlay: (card: Card) => void;
  onImpressed: (selectionId: number) => void;
  onChanged: () => void;
}

export function VideoCard({ card: initial, onPlay, onImpressed, onChanged }: VideoCardProps) {
  const ref = useRef<HTMLDivElement>(null);
  // reveal swaps in the full card returned by the server (AC-16: packaging
  // only ever arrives via the logged reveal)
  const [card, setCard] = useState(initial);
  const [reasonOpen, setReasonOpen] = useState(false);
  const [liked, setLiked] = useState(initial.liked);
  const [queued, setQueued] = useState(initial.in_queue);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [receiptOpen, setReceiptOpen] = useState(false);

  useEffect(() => {
    setCard(initial);
    setLiked(initial.liked);
    setQueued(initial.in_queue);
  }, [initial]);

  // Impression = card actually in the viewport for >= 1s (§5)
  useEffect(() => {
    const el = ref.current;
    if (!el || card.selection_id === null) return;
    let timer: number | undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            timer = window.setTimeout(() => onImpressed(card.selection_id!), 1000);
          } else if (timer) {
            clearTimeout(timer);
            timer = undefined;
          }
        }
      },
      { threshold: 0.5 },
    );
    observer.observe(el);
    return () => {
      if (timer) clearTimeout(timer);
      observer.disconnect();
    };
  }, [card.selection_id, onImpressed]);

  const revealAndGet = async (): Promise<Card> => {
    if (!card.blind) return card;
    const result = await api<{ card: Card }>(
      `/selections/${card.selection_id}/reveal`,
      { method: "POST" },
    );
    setCard(result.card);
    return result.card;
  };

  const play = async () => {
    const full = await revealAndGet();
    await api("/interactions", {
      method: "POST",
      body: {
        video_id: full.video_id,
        selection_id: full.selection_id,
        kind: "play",
      },
    });
    onPlay(full);
  };

  const toggleLike = async () => {
    await api("/feedback", {
      method: "POST",
      body: {
        video_id: card.video_id,
        kind: liked ? "unlike" : "like",
        selection_id: card.selection_id,
      },
    });
    setLiked(!liked);
  };

  const notInterested = async (reason: string) => {
    await api("/feedback", {
      method: "POST",
      body: {
        video_id: card.video_id,
        kind: "not_interested",
        reason: reason || null,
        selection_id: card.selection_id,
      },
    });
    onChanged();
  };

  const [clickbaited, setClickbaited] = useState(false);
  const flagClickbait = async () => {
    if (clickbaited) return;               // one signal per card render
    await api("/feedback", {
      method: "POST",
      body: {
        video_id: card.video_id,
        kind: "clickbait",
        selection_id: card.selection_id,
      },
    });
    setClickbaited(true);
  };

  const blockChannel = async () => {
    const ok = await confirmModal({
      title: "Block channel",
      message: `Never show anything from ${card.channel_title ?? "this channel"} again?`,
      okLabel: "Block",
      danger: true,
    });
    if (!ok) return;
    await api("/feedback", {
      method: "POST",
      body: {
        video_id: card.video_id,
        kind: "block_channel",
        selection_id: card.selection_id,
      },
    });
    onChanged();
  };

  const addToQueue = async () => {
    await api("/queue", {
      method: "POST",
      body: { video_id: card.video_id, selection_id: card.selection_id },
    });
    setQueued(true);
  };

  const toggleReceipt = async () => {
    if (receiptOpen) {
      setReceiptOpen(false);
      return;
    }
    if (!receipt && card.selection_id !== null) {
      setReceipt(await api<Receipt>(`/selections/${card.selection_id}/receipt`));
    }
    setReceiptOpen(true);
  };

  return (
    <div className="card" ref={ref}>
      {card.blind ? (
        <div className="thumb blind" onClick={play} title="Blind audition — title only">
          ?
        </div>
      ) : card.thumbnail_url ? (
        <img className="thumb" src={card.thumbnail_url} onClick={play} alt="" />
      ) : (
        <div className="thumb blind" onClick={play}>
          ▶
        </div>
      )}
      <div className="body">
        <div className="badges">
          {card.slot_class === "exploration" && (
            <span className="badge exploration">exploration</span>
          )}
          {card.blind && <span className="badge blind">blind audition</span>}
        </div>
        <div className="title" onClick={play}>
          {card.title ?? "(untitled)"}
        </div>
        <div className="meta">
          {!card.blind && card.channel_title && <span>{card.channel_title}</span>}
          {!card.blind && <span>{formatDuration(card.duration_seconds)}</span>}
          {!card.blind && card.published_at && (
            <span
              className={`age${ageClass(card.published_at)}`}
              title={new Date(card.published_at).toLocaleString()}
            >
              {formatAge(card.published_at)}
            </span>
          )}
        </div>
        <div className="actions">
          <button className="primary" onClick={play}>
            {card.blind ? "Reveal & play" : "Play"}
          </button>
          {/* DEC-21: the blind outcomes are play or DISMISS — dismissal
              works without ever revealing the video */}
          {card.blind && (
            <button
              onClick={async () => {
                await api(`/selections/${card.selection_id}/blind-dismiss`, {
                  method: "POST",
                });
                onChanged();
              }}
            >
              Dismiss
            </button>
          )}
          {/* every other action needs the video identity — hidden until reveal */}
          {!card.blind && (
            <>
              <button className={liked ? "active" : ""} onClick={toggleLike}>
                {liked ? "♥ Liked" : "♥ Like"}
              </button>
              <button disabled={queued} onClick={addToQueue}>
                {queued ? "Queued" : "+ Queue"}
              </button>
              <button onClick={() => setReasonOpen(true)}>Not interested</button>
              {/* AC-37: the explicit clickbait signal feeding
                  promise-vs-delivery */}
              <button
                className={clickbaited ? "active" : ""}
                onClick={flagClickbait}
                title="Title/thumbnail oversold the video"
              >
                {clickbaited ? "Flagged" : "Clickbait"}
              </button>
              {card.channel_title && (
                <button className="danger" onClick={blockChannel}>
                  Block
                </button>
              )}
            </>
          )}
          {card.selection_id !== null && (
            <button onClick={toggleReceipt}>Why this?</button>
          )}
        </div>
        {receiptOpen && receipt && (
          <div className="receipt">
            {receipt.explanation && <div>{receipt.explanation}</div>}
            {receipt.anchors && receipt.anchors.length > 0 && (
              <div>
                Because you watched:{" "}
                {receipt.anchors.map((a) => a.title).filter(Boolean).join(" · ")}
              </div>
            )}
            {receipt.steering && receipt.steering.length > 0 && (
              <div>
                Steering: {receipt.steering.map((s) => s.summary).join(" · ")}
              </div>
            )}
            {receipt.overpromise && (
              <div className="muted">
                Channel overpromise penalty −
                {receipt.overpromise.penalty_applied} (
                {receipt.overpromise.clickbait_count} clickbait flags over{" "}
                {receipt.overpromise.watches} watches)
              </div>
            )}
          </div>
        )}
      </div>
      {reasonOpen && (
        <ReasonModal
          title="Not interested"
          prompt="This removes the video from current and future editions."
          onSubmit={notInterested}
          onClose={() => setReasonOpen(false)}
        />
      )}
    </div>
  );
}
