export interface HumanReviewBriefing {
  reason: string;
  summary: string;
  known_facts: string[];
  conflicts: string[];
  unresolved_questions: string[];
  recommended_next_action: string;
  confidence?: number | null;
}

export interface HumanReview {
  review_id: string;
  claim_id: string;
  status: 'pending' | 'approved' | 'correction_requested' | 'expired';
  reason: string;
  briefing: HumanReviewBriefing;
  expires_at: string;
  decision_at?: string | null;
}

export interface HumanReviewDecision {
  review_id: string;
  claim_id: string;
  status: 'approved' | 'correction_requested';
  event_id: string;
  message: string;
  duplicate: boolean;
}
