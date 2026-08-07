let csrfToken: string | null = null;

export function getCsrf(): string | null {
  return csrfToken;
}

export async function api<T = unknown>(
  path: string,
  // body is an OBJECT — api() stringifies it. Typing it `object` makes a
  // pre-stringified body (double-encoding, the 422 class of bug) a type error.
  options: { method?: string; body?: object } = {},
): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const response = await fetch(`/api${path}`, {
    method,
    headers,
    credentials: "include",
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  if (response.status === 401) throw new UnauthenticatedError();
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const data = await response.json();
      if (data.detail) detail = String(data.detail);
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export class UnauthenticatedError extends Error {
  constructor() {
    super("not signed in");
  }
}

export async function loadMe(): Promise<{
  user: { user_id: number; email: string | null; display_name: string | null };
} | null> {
  try {
    const me = await api<{
      user: { user_id: number; email: string | null; display_name: string | null };
      csrf_token: string;
    }>("/me");
    csrfToken = me.csrf_token;
    return { user: me.user };
  } catch (error) {
    if (error instanceof UnauthenticatedError) return null;
    throw error;
  }
}
