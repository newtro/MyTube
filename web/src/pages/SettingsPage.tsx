import { useEffect, useState } from "react";
import { api } from "../api";
import type { UserSettings } from "../types";

interface SliderProps {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  hint?: string;
  onChange: (value: number) => void;
}

function Slider({ label, value, min, max, step = 1, hint, onChange }: SliderProps) {
  return (
    <div className="setting">
      <label>
        <span>{label}</span>
        <span className="value">{value}</span>
      </label>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

export function SettingsPage() {
  const [settings, setSettings] = useState<UserSettings | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void api<UserSettings>("/settings").then(setSettings);
  }, []);

  if (!settings) return <p className="center-note">Loading…</p>;

  const update = (patch: Partial<UserSettings>) => {
    setSettings({ ...settings, ...patch });
    setSaved(false);
  };

  const save = async () => {
    setError(null);
    try {
      const result = await api<UserSettings>("/settings", {
        method: "PUT",
        body: settings,
      });
      setSettings(result);
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="page">
      <div className="edition-head">
        <h1>Feed mix</h1>
        <span className="muted">
          changes apply to the next composition (DEC-27)
        </span>
      </div>
      {error && <div className="banner">{error}</div>}
      <div className="settings-grid">
        <Slider
          label="Edition size"
          value={settings.edition_size}
          min={10}
          max={30}
          hint="~20 cards; finite and completable by design"
          onChange={(v) => update({ edition_size: v })}
        />
        <Slider
          label="Exploration slots"
          value={settings.exploration_slots}
          min={2}
          max={10}
          hint="minimum 2 — the future learned ranker feeds on these (DEC-10)"
          onChange={(v) => update({ exploration_slots: v })}
        />
        <Slider
          label="Subscription quota"
          value={settings.subscription_quota}
          min={0}
          max={30}
          onChange={(v) => update({ subscription_quota: v })}
        />
        <Slider
          label="Discovery quota"
          value={settings.discovery_quota}
          min={0}
          max={30}
          hint="activates with Phase 3 discovery engines"
          onChange={(v) => update({ discovery_quota: v })}
        />
        <Slider
          label="Wildcard quota"
          value={settings.wildcard_quota}
          min={0}
          max={30}
          hint="activates with Phase 3 trending"
          onChange={(v) => update({ wildcard_quota: v })}
        />
        <Slider
          label="Carry-over staleness penalty (per day)"
          value={settings.staleness_penalty_per_day}
          min={0}
          max={0.2}
          step={0.01}
          hint="how fast unwatched carry-over cards fade (DEC-23)"
          onChange={(v) => update({ staleness_penalty_per_day: v })}
        />
        <Slider
          label="Duration-fit weight"
          value={settings.duration_fit_weight}
          min={0}
          max={0.3}
          step={0.01}
          hint="demote videos far from your typical watched length (0 = off)"
          onChange={(v) => update({ duration_fit_weight: v })}
        />
        <Slider
          label="Compose hour (local)"
          value={settings.compose_hour}
          min={0}
          max={23}
          onChange={(v) => update({ compose_hour: v })}
        />
        <div className="setting">
          <label>
            <span>Timezone (IANA)</span>
          </label>
          <input
            type="text"
            value={settings.timezone}
            onChange={(e) => update({ timezone: e.target.value })}
          />
          <div className="hint">e.g. America/New_York — drives the daily edition date</div>
        </div>
        <div>
          <button className="primary" onClick={save}>
            Save
          </button>
          {saved && <span style={{ marginLeft: 10 }}>Saved ✓</span>}
        </div>
      </div>
    </div>
  );
}
