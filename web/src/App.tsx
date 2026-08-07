import { useEffect, useState } from "react";
import {
  BrowserRouter,
  NavLink,
  Route,
  Routes,
} from "react-router-dom";
import { api, loadMe } from "./api";
import { EditionPage } from "./pages/EditionPage";
import { OpsPage } from "./pages/OpsPage";
import { QueuePage } from "./pages/QueuePage";
import { SettingsPage } from "./pages/SettingsPage";
import { SteeringPage } from "./pages/SteeringPage";
import { TastePage } from "./pages/TastePage";

interface Me {
  user: { user_id: number; email: string | null; display_name: string | null };
}

function SignIn() {
  return (
    <div className="signin">
      <h1>MyTube</h1>
      <p style={{ color: "var(--muted)" }}>
        Your feed, your rules. Sign in with the allowlisted Google account.
      </p>
      <a className="primary" href="/api/auth/login">
        <button className="primary">Sign in with Google</button>
      </a>
    </div>
  );
}

function ConnectBanner() {
  const [status, setStatus] = useState<{
    connected: boolean;
    needs_reconnect?: boolean;
    via?: string | null;
    account?: string | null;
  } | null>(null);

  const reload = () =>
    api<{
      connected: boolean;
      needs_reconnect?: boolean;
      via?: string | null;
      account?: string | null;
    }>("/connect/status").then(setStatus);

  useEffect(() => {
    void reload();
  }, []);

  if (!status) return null;
  if (!status.connected) {
    return (
      <div className="banner">
        {status.needs_reconnect
          ? "YouTube access was revoked or expired — sync is paused. "
          : "YouTube isn't connected — subscriptions and likes can't sync. "}
        <a href="/api/connect/start">
          {status.needs_reconnect
            ? "Reconnect YouTube (read-only)"
            : "Connect YouTube (read-only)"}
        </a>
      </div>
    );
  }
  // FLOW-1b: the connected account may differ from the sign-in account —
  // always show which one it is
  return (
    <div className="banner info">
      YouTube connected{status.account ? ` as ${status.account}` : ""}
      {status.via === "cli-token" ? " (via CLI token)" : ""}.{" "}
      <a
        href="#disconnect"
        onClick={async (e) => {
          e.preventDefault();
          await api("/connect/disconnect", { method: "POST" });
          await reload();
        }}
      >
        Disconnect
      </a>
    </div>
  );
}

export default function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);

  useEffect(() => {
    void loadMe().then(setMe);
  }, []);

  if (me === undefined) return <p className="center-note">Loading…</p>;
  if (me === null) return <SignIn />;

  return (
    <BrowserRouter>
      <nav className="topnav">
        <span className="brand">MyTube</span>
        <NavLink
          to="/"
          end
          className={({ isActive }) => `navlink${isActive ? " active" : ""}`}
        >
          Edition
        </NavLink>
        <NavLink
          to="/queue"
          className={({ isActive }) => `navlink${isActive ? " active" : ""}`}
        >
          Watch later
        </NavLink>
        <NavLink
          to="/steering"
          className={({ isActive }) => `navlink${isActive ? " active" : ""}`}
        >
          Steering
        </NavLink>
        <NavLink
          to="/taste"
          className={({ isActive }) => `navlink${isActive ? " active" : ""}`}
        >
          Taste
        </NavLink>
        <NavLink
          to="/ops"
          className={({ isActive }) => `navlink${isActive ? " active" : ""}`}
        >
          Ops
        </NavLink>
        <NavLink
          to="/settings"
          className={({ isActive }) => `navlink${isActive ? " active" : ""}`}
        >
          Settings
        </NavLink>
        <span className="spacer" />
        <span style={{ color: "var(--muted)", fontSize: 13 }}>
          {me.user.email}
        </span>
        <button
          onClick={async () => {
            await api("/auth/logout", { method: "POST" });
            setMe(null);
          }}
        >
          Sign out
        </button>
      </nav>
      <ConnectBanner />
      <Routes>
        <Route path="/" element={<EditionPage />} />
        <Route path="/queue" element={<QueuePage />} />
        <Route path="/steering" element={<SteeringPage />} />
        <Route path="/taste" element={<TastePage />} />
        <Route path="/ops" element={<OpsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Routes>
    </BrowserRouter>
  );
}
