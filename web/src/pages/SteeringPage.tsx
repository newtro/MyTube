import { useEffect, useState } from "react";
import { api } from "../api";
import { confirmModal } from "../components/Modal";

interface Term {
  text: string;
  polarity: number;
  weight: number;
}

interface Directive {
  directive_id: number;
  raw_text: string;
  summary: string | null;
  created_at: string;
  expires_at: string;
  cancelled_at: string | null;
  active: boolean;
  terms: Term[];
}

function TermChips({ terms }: { terms: Term[] }) {
  return (
    <span className="term-chips">
      {terms.map((t, i) => (
        <span
          key={i}
          className={`chip ${t.polarity > 0 ? "chip-boost" : "chip-suppress"}`}
          title={`weight ${t.weight}`}
        >
          {t.polarity > 0 ? "+" : "−"} {t.text}
        </span>
      ))}
    </span>
  );
}

export function SteeringPage() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lists, setLists] = useState<{
    active: Directive[];
    past: Directive[];
  } | null>(null);

  const reload = () =>
    api<{ active: Directive[]; past: Directive[] }>("/steering").then(setLists);

  useEffect(() => {
    void reload();
  }, []);

  const submit = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api("/steering", {
        method: "POST",
        body: { text: text.trim() },
      });
      setText("");
      await reload();
    } catch (e) {
      // AC-13: parse/embed failure is an inline error; nothing was stored
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async (d: Directive) => {
    const ok = await confirmModal({
      title: "Cancel directive",
      message: `Stop steering "${d.summary || d.raw_text}"? Its influence and seeded searches end immediately.`,
      okLabel: "Cancel directive",
      danger: true,
    });
    if (!ok) return;
    await api(`/steering/${d.directive_id}/cancel`, { method: "POST" });
    await reload();
  };

  return (
    <main className="page">
      <h2>Steering</h2>
      <p className="muted">
        Tell the ranker what you want more or less of, in plain language.
        Directives expire automatically (each shows its date below) unless
        cancelled, and stack additively.
      </p>
      <div className="steering-form">
        <textarea
          value={text}
          placeholder='e.g. "more hand-tool woodworking, less election politics this week"'
          maxLength={1000}
          rows={3}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void submit();
          }}
        />
        <button className="primary" disabled={busy || !text.trim()} onClick={submit}>
          {busy ? "Parsing…" : "Apply"}
        </button>
      </div>
      {error && <div className="banner">{error}</div>}

      <h3>Active</h3>
      {lists && lists.active.length === 0 && (
        <p className="muted">No active directives.</p>
      )}
      {lists?.active.map((d) => (
        <div key={d.directive_id} className="card directive">
          <div className="directive-head">
            <strong>{d.summary || d.raw_text}</strong>
            <button onClick={() => void cancel(d)}>Cancel</button>
          </div>
          <TermChips terms={d.terms} />
          <div className="muted">
            expires {new Date(d.expires_at).toLocaleString()}
          </div>
        </div>
      ))}

      <h3>Past</h3>
      {lists && lists.past.length === 0 && (
        <p className="muted">Nothing yet.</p>
      )}
      {lists?.past.map((d) => (
        <div key={d.directive_id} className="card directive past">
          <div className="directive-head">
            <span>{d.summary || d.raw_text}</span>
            <span className="muted">
              {d.cancelled_at ? "cancelled" : "expired"}
            </span>
          </div>
          <TermChips terms={d.terms} />
        </div>
      ))}
    </main>
  );
}
