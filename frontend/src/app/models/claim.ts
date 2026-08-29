export interface ClaimAcceptedResponse {
  claim_id: string;
  status: string;
  event_id: string;
  message: string;
}

export interface DocumentAcceptedResponse {
  claim_id: string;
  document_id: string;
  status: string;
  event_id: string;
}

export interface MissingDocument {
  type?: string;
  document_type?: string;
  reason?: string;
  source_requirement?: string;
}

export interface ClaimantEvidenceRequest {
  document_type: string;
  label: string;
  instruction: string;
  satisfies_requirements: string[];
  replacement_required: boolean;
  requested_action_id?: string;
}

export interface ClaimantActionDisplay {
  title: string;
  explanation: string;
}

export interface InspectionAppointment {
  appointment_id: string;
  inspection_type: 'virtual' | 'physical';
  status: string;
  scheduled_start: string;
  scheduled_end: string;
  location_type: string;
  location_details?: string | null;
}

export interface ClaimSummary {
  claim_id: string;
  status: string;
  intake_priority?: string | null;
  missing_documents: MissingDocument[];
  requested_evidence: ClaimantEvidenceRequest[];
  requested_actions?: RequestedAction[];
  action_display?: ClaimantActionDisplay | null;
  voice_correction_processing?: {
    requested_action_id: string;
    status: 'processing' | 'accepted' | 'unusable';
    message?: string | null;
  } | null;
  manual_handling?: boolean;
  inspection?: InspectionAppointment | null;
  updated_at: string;
}

export interface EnterTextRequestedAction {
  action_type: 'enter_text';
  action_id: string;
  field_name: string;
  instruction: string;
  review_id: string;
}

export interface UploadDocumentRequestedAction {
  action_type: 'upload_document';
  action_id: string;
  review_id: string;
  document_type: string;
  instruction: string;
  replaces_document_id?: string | null;
}

export type RequestedAction = EnterTextRequestedAction | UploadDocumentRequestedAction;

export interface ClaimSubmission {
  incidentDescription: string;
  policyNumberHint?: string;
  evidenceFiles: File[];
}
