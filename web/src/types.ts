export interface Card {
  selection_id: number | null;
  position: number;
  slot_class: string;
  blind: boolean;
  /** null while a blind card is unrevealed (server withholds it, AC-16) */
  video_id: string | null;
  title: string | null;
  duration_seconds: number | null;
  dismissed: boolean;
  replaced: boolean;
  in_queue: boolean;
  liked: boolean;
  channel_id?: string | null;
  channel_title?: string | null;
  thumbnail_url?: string | null;
  view_count?: number | null;
  published_at?: string | null;
}

export interface Edition {
  edition_id: number;
  local_date: string;
  generation_no: number;
  note: string | null;
  cards: Card[];
}

export interface AppStatus {
  ready: boolean;
  subscriptions: number;
  hydrated_videos: number;
  scored_videos: number;
}

export interface UserSettings {
  timezone: string;
  compose_hour: number;
  edition_size: number;
  exploration_slots: number;
  subscription_quota: number;
  discovery_quota: number;
  wildcard_quota: number;
  staleness_penalty_per_day: number;
  duration_fit_weight: number;
  freshness_weight: number;
  freshness_half_life_days: number;
  freshness_adaptive: boolean;
  llm_model: string | null;
  llm_model_choices?: string[];
  llm_model_default?: string;
}

export interface QueueItem {
  video_id: string;
  position: number;
  title: string | null;
  duration_seconds: number | null;
  thumbnail_url: string | null;
  channel_title: string | null;
}
