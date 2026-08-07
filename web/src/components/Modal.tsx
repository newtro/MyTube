import { ReactNode, useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

interface ModalProps {
  title: string;
  children: ReactNode;
  onClose: () => void;
}

export function Modal({ title, children, onClose }: ModalProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        {children}
      </div>
    </div>
  );
}

interface ConfirmOptions {
  title: string;
  message: string;
  okLabel?: string;
  danger?: boolean;
}

/** Promise-based themed confirm — native confirm()/alert() are banned. */
export function confirmModal(options: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    const done = (value: boolean) => {
      root.unmount();
      host.remove();
      resolve(value);
    };
    function ConfirmView() {
      useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
          if (e.key === "Enter") done(true);
        };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
      }, []);
      return (
        <Modal title={options.title} onClose={() => done(false)}>
          <p>{options.message}</p>
          <div className="row">
            <button onClick={() => done(false)}>Cancel</button>
            <button
              className={options.danger ? "danger" : "primary"}
              onClick={() => done(true)}
            >
              {options.okLabel ?? "Confirm"}
            </button>
          </div>
        </Modal>
      );
    }
    root.render(<ConfirmView />);
  });
}

interface ReasonModalProps {
  title: string;
  prompt: string;
  onSubmit: (reason: string) => void;
  onClose: () => void;
}

export function ReasonModal({ title, prompt, onSubmit, onClose }: ReasonModalProps) {
  const [reason, setReason] = useState("");
  const submit = useCallback(() => {
    onSubmit(reason.trim());
    onClose();
  }, [reason, onSubmit, onClose]);
  return (
    <Modal title={title} onClose={onClose}>
      <p>{prompt}</p>
      <textarea
        autoFocus
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="optional reason — trains the rules layer"
      />
      <div className="row">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={submit}>
          Not interested
        </button>
      </div>
    </Modal>
  );
}
