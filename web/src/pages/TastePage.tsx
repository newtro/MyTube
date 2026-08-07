import { useEffect, useState } from "react";
import { api } from "../api";

interface Cluster {
  label: string;
  size: number;
  top_titles: string[];
  recent_30d: number;
  prior_30d: number;
  drift: number;
}

interface Dashboard {
  model_id: string;
  taste_items: number;
  clustered: number;
  clusters: Cluster[];
  directives: {
    active: { directive_id: number; summary: string | null; raw_text: string; expires_at: string }[];
    past: { directive_id: number; summary: string | null; raw_text: string; cancelled_at: string | null }[];
  };
  ranker_beliefs: {
    top_scored: { video_id: string; title: string; score: number }[];
    knn_k: number;
    negative_penalty_weight: number;
    steering_weight: number;
    overpromise_penalty_weight: number;
  };
}

export function TastePage() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Dashboard>("/dashboard/taste")
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  if (error) return <main className="page"><div className="banner">{error}</div></main>;
  if (!data) return <main className="page"><p className="center-note">Loading…</p></main>;

  const maxSize = Math.max(1, ...data.clusters.map((c) => c.size));
  return (
    <main className="page">
      <h2>Taste</h2>
      <p className="muted">
        {data.taste_items} taste items ({data.clustered} clustered) ·{" "}
        model {data.model_id}
      </p>

      <h3>Interest clusters</h3>
      {data.clusters.length === 0 && (
        <p className="muted">No clusters yet — import history and let the embedder run.</p>
      )}
      {data.clusters.map((c, i) => (
        <div key={i} className="card cluster">
          <div className="cluster-head">
            <strong>{c.label}</strong>
            <span className="muted">
              {c.size} items ·{" "}
              {c.drift > 0 ? `▲ +${c.drift}` : c.drift < 0 ? `▼ ${c.drift}` : "steady"}{" "}
              last 30d
            </span>
          </div>
          <div className="bar-track">
            <div
              className="bar-fill"
              style={{ width: `${(100 * c.size) / maxSize}%` }}
            />
          </div>
          <div className="muted">{c.top_titles.join(" · ")}</div>
        </div>
      ))}

      <h3>Steering history</h3>
      {data.directives.active.map((d) => (
        <div key={d.directive_id} className="card directive">
          <strong>{d.summary || d.raw_text}</strong>{" "}
          <span className="muted">
            active · expires {new Date(d.expires_at).toLocaleDateString()}
          </span>
        </div>
      ))}
      {data.directives.past.map((d) => (
        <div key={d.directive_id} className="card directive past">
          {d.summary || d.raw_text}{" "}
          <span className="muted">{d.cancelled_at ? "cancelled" : "expired"}</span>
        </div>
      ))}
      {data.directives.active.length + data.directives.past.length === 0 && (
        <p className="muted">No directives yet.</p>
      )}

      <h3>What the ranker believes right now</h3>
      <p className="muted">
        kNN k={data.ranker_beliefs.knn_k} · negative penalty{" "}
        {data.ranker_beliefs.negative_penalty_weight} · steering ×
        {data.ranker_beliefs.steering_weight} · overpromise ×
        {data.ranker_beliefs.overpromise_penalty_weight}
      </p>
      <ol>
        {data.ranker_beliefs.top_scored.map((v) => (
          <li key={v.video_id}>
            {v.title} <span className="muted">({v.score.toFixed(3)})</span>
          </li>
        ))}
      </ol>
    </main>
  );
}
