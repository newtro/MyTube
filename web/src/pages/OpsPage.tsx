import { useEffect, useState } from "react";
import { api } from "../api";

interface CategoryRow {
  reserved: number;
  charged: number;
  hard_cap: number;
}

interface OpsSummary {
  quota: {
    quota_day: string;
    categories: Record<string, CategoryRow>;
    total: { reserved: number; charged: number; budget: number; safety_reserve: number };
  };
  jobs: {
    job_name: string;
    state: string;
    attempt: number;
    started_at: string | null;
    finished_at: string | null;
    last_success_at: string | null;
    error: string | null;
  }[];
  recent_errors: {
    job_name: string;
    state: string;
    attempt: number;
    finished_at: string | null;
    error: string;
  }[];
  backlogs: Record<string, number>;
  alerts: { kind: string; subject: string; message: string; created_at: string }[];
}

export function OpsPage() {
  const [data, setData] = useState<OpsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = () =>
    api<OpsSummary>("/ops/summary")
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));

  useEffect(() => {
    void reload();
    const t = setInterval(() => void reload(), 30_000);
    return () => clearInterval(t);
  }, []);

  if (error) return <main className="page"><div className="banner">{error}</div></main>;
  if (!data) return <main className="page"><p className="center-note">Loading…</p></main>;

  const used = data.quota.total.reserved + data.quota.total.charged;
  return (
    <main className="page">
      <h2>Ops</h2>
      <p className="muted">
        Quota day {data.quota.quota_day} · {used}/{data.quota.total.budget} units
        used (+{data.quota.total.safety_reserve} safety reserve, never scheduled)
      </p>

      <h3>Quota ledger vs caps</h3>
      <table className="ops-table">
        <thead>
          <tr><th>Category</th><th>Charged</th><th>Reserved</th><th>Hard cap</th><th /></tr>
        </thead>
        <tbody>
          {Object.entries(data.quota.categories).map(([name, c]) => {
            const pct = Math.min(100, (100 * (c.charged + c.reserved)) / c.hard_cap);
            return (
              <tr key={name}>
                <td>{name}</td>
                <td>{c.charged}</td>
                <td>{c.reserved}</td>
                <td>{c.hard_cap}</td>
                <td style={{ width: "40%" }}>
                  <div className="bar-track">
                    <div
                      className={`bar-fill${pct > 85 ? " warn" : ""}`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <h3>Jobs</h3>
      <table className="ops-table">
        <thead>
          <tr><th>Job</th><th>State</th><th>Attempt</th><th>Last success</th><th>Last error</th></tr>
        </thead>
        <tbody>
          {data.jobs.map((j) => (
            <tr key={j.job_name}>
              <td>{j.job_name}</td>
              <td>
                <span className={`state state-${j.state}`}>{j.state}</span>
              </td>
              <td>{j.attempt}</td>
              <td className="muted">
                {j.last_success_at
                  ? new Date(j.last_success_at).toLocaleString()
                  : "never"}
              </td>
              <td className="muted error-cell">{j.error || ""}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Recent job errors</h3>
      {data.recent_errors.length === 0 && (
        <p className="muted">No recorded errors.</p>
      )}
      {data.recent_errors.map((e, i) => (
        <div key={i} className="card alert">
          <strong>{e.job_name}</strong>{" "}
          <span className="muted">
            attempt {e.attempt} ({e.state}
            {e.finished_at ? `, ${new Date(e.finished_at).toLocaleString()}` : ""})
          </span>
          <div className="muted">{e.error}</div>
        </div>
      ))}

      <h3>Backlogs</h3>
      <table className="ops-table">
        <tbody>
          {Object.entries(data.backlogs).map(([name, n]) => (
            <tr key={name}>
              <td>{name.replaceAll("_", " ")}</td>
              <td>{n}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Alerts</h3>
      {data.alerts.length === 0 && <p className="muted">No unresolved alerts.</p>}
      {data.alerts.map((a, i) => (
        <div key={i} className="card alert">
          <strong>{a.kind}</strong> {a.subject}
          <div className="muted">{a.message}</div>
          <div className="muted">{new Date(a.created_at).toLocaleString()}</div>
        </div>
      ))}
    </main>
  );
}
