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
  generation?: number;
  recommended_remediation?: RecommendedRemediation;
  ai_recommendation?: string;
  claim_snapshot?: Record<string, string | boolean | null>;
  evidence_comparison?: Array<{ source: string; finding: string }>;
  resolution_history?: string[];
  expires_at: string;
  decision_at?: string | null;
  checkpoint_status?: string | null;
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
