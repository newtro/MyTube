import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { Player } from "../components/Player";
import type { Card, QueueItem } from "../types";

export function QueuePage() {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [playing, setPlaying] = useState<Card | null>(null);

  const reload = useCallback(async () => {
    const data = await api<{ items: QueueItem[] }>("/queue");
    setItems(data.items);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const remove = async (videoId: string) => {
    await api(`/queue/${videoId}`, { method: "DELETE" });
    await reload();
  };

  const move = async (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= items.length) return;
    const order = [...items];
    [order[index], order[target]] = [order[target], order[index]];
    setItems(order);
    await api("/queue/order", {
      method: "PUT",
      body: { video_ids: order.map((i) => i.video_id) },
    });
  };

  const play = async (item: QueueItem) => {
    // playing from the queue emits standard telemetry (AC-34) — selection_id
    // is null because queue plays aren't edition cards
    await api("/interactions", {
      method: "POST",
      body: { video_id: item.video_id, selection_id: null, kind: "play" },
    });
    setPlaying({
      selection_id: null,
      position: 0,
      slot_class: "queue",
      blind: false,
      video_id: item.video_id,
      title: item.title,
      duration_seconds: item.duration_seconds,
      dismissed: false,
      replaced: false,
      in_queue: true,
      liked: false,
      channel_title: item.channel_title,
      thumbnail_url: item.thumbnail_url,
    });
  };

  return (
    <div className="page">
      <div className="edition-head">
        <h1>Watch later</h1>
        <span className="muted">{items.length} videos</span>
      </div>
      <div className="queue-list">
        {items.map((item, index) => (
          <div className="queue-item" key={item.video_id}>
            {item.thumbnail_url ? (
              <img src={item.thumbnail_url} alt="" onClick={() => play(item)} />
            ) : (
              <div style={{ width: 120 }} />
            )}
            <div className="qtitle">
              <div>{item.title ?? "(untitled)"}</div>
              <div style={{ color: "var(--muted)", fontSize: 12 }}>
                {item.channel_title}
              </div>
            </div>
            <button onClick={() => move(index, -1)} disabled={index === 0}>
              ↑
            </button>
            <button
              onClick={() => move(index, 1)}
              disabled={index === items.length - 1}
            >
              ↓
            </button>
            <button className="primary" onClick={() => play(item)}>
              Play
            </button>
            <button className="danger" onClick={() => remove(item.video_id)}>
              Remove
            </button>
          </div>
        ))}
        {!items.length && (
          <p className="center-note">Queue is empty — add videos from the edition.</p>
        )}
      </div>
      {playing && (
        <Player
          card={playing}
          onClose={() => setPlaying(null)}
          onUnavailable={async (card) => {
            setPlaying(null);
            // FLOW-20 for queue plays: server-verified tombstone + removal
            await api(`/queue/${card.video_id}/unavailable`, { method: "POST" });
            await reload();
          }}
        />
      )}
    </div>
  );
}
