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
  status: 'pending' | 'approved' | 'correction_requested' | 'manual_handling' | 'expired';
  reason: string;
  briefing: HumanReviewBriefing;
  source_references?: EvidenceSourceReference[];
  supporting_documents?: SupportingDocument[];
  claimant_voice_updates?: ClaimantVoiceUpdate[];
  generation?: number;
  recommended_remediation?: RecommendedRemediation;
  ai_recommendation?: string;
  claim_snapshot?: Record<string, string | boolean | null>;
  evidence_comparison?: Array<{
    source: string;
    finding: string;
    document_type?: string;
  }>;
  resolution_history?: string[];
  expires_at: string;
  decision_at?: string | null;
  checkpoint_status?: string | null;
}

export interface ClaimantVoiceUpdate {
  source_label: 'Claimant voice response';
  incident_date?: string | null;
  incident_time?: string | null;
  incident_description?: string | null;
  injury_mentioned: boolean;
  injury_description?: string | null;
  contributed_to_decision: boolean;
}

export interface SupportingDocument {
  document_id: string;
  filename: string;
  document_type: string;
  status: string;
}

export interface EvidenceSourceReference {
  filename: string;
}

export interface RecommendedRemediation {
  type: 'enter_text' | 'upload_document';
  label: string;
  instruction: string;
  field_name?: string | null;
  document_type?: string | null;
  can_request: boolean;
}

export interface HumanReviewDecision {
  review_id: string;
  claim_id: string;
  status: 'approved' | 'correction_requested' | 'manual_handling';
  event_id: string;
  message: string;
  duplicate: boolean;
}

export interface HumanReviewEvidenceRequest {
  document_type: string;
  instruction: string;
  replaces_document_id?: string | null;
}
