export interface ClaimEvent {
  timestamp: string;
  action: string;
  actor: string;
  from_status?: string | null;
  to_status?: string | null;
  details: Record<string, unknown>;
  correlation_id?: string | null;
}

export interface PresentedEvent {
  key: string;
  title: string;
  description: string;
  timestamp: string;
  event: ClaimEvent;
}

export interface AgentActivityItem {
  key: string;
  agent: string;
  description: string;
  timestamp: string;
}
