import { HttpClient, HttpEvent } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { environment } from '../../../environments/environment';
import {
  ClaimAcceptedResponse,
  ClaimSubmission,
  ClaimSummary,
  DocumentAcceptedResponse,
} from '../../models/claim';
import { ClaimEvent } from '../../models/claim-event';
import { HumanReview, HumanReviewDecision } from '../../models/human-review';

@Injectable({ providedIn: 'root' })
export class ClaimApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiBaseUrl.replace(/\/$/, '');

  submitClaim(
    submission: ClaimSubmission,
    idempotencyKey: string,
  ): Observable<HttpEvent<ClaimAcceptedResponse>> {
    const body = new FormData();
    body.append('incident_description', submission.incidentDescription);
    if (submission.policyNumberHint) {
      body.append('policy_number_hint', submission.policyNumberHint);
    }
    for (const photo of submission.damagePhotos) body.append('files', photo);
    if (submission.policeReport) body.append('files', submission.policeReport);
    if (submission.audio) body.append('files', submission.audio);
    return this.http.post<ClaimAcceptedResponse>(`${this.baseUrl}/claims`, body, {
      headers: { 'X-Idempotency-Key': idempotencyKey },
      observe: 'events',
      reportProgress: true,
    });
  }

  getClaim(claimId: string): Observable<ClaimSummary> {
    return this.http.get<ClaimSummary>(`${this.baseUrl}/claims/${claimId}`);
  }

  getClaimEvents(claimId: string): Observable<ClaimEvent[]> {
    return this.http.get<ClaimEvent[]>(`${this.baseUrl}/claims/${claimId}/events`);
  }

  uploadDocument(
    claimId: string,
    documentType: string,
    file: File,
    requestedActionId?: string,
    idempotencyKey?: string,
  ): Observable<DocumentAcceptedResponse> {
    const body = new FormData();
    body.append('document_type', documentType);
    body.append('file', file);
    if (requestedActionId) body.append('requested_action_id', requestedActionId);
    return this.http.post<DocumentAcceptedResponse>(
      `${this.baseUrl}/claims/${claimId}/documents`,
      body,
      idempotencyKey ? { headers: { 'X-Idempotency-Key': idempotencyKey } } : {},
    );
  }

  getHumanReview(token: string): Observable<HumanReview> {
    return this.http.get<HumanReview>(
      `${this.baseUrl}/reviews/current`,
      { headers: { 'X-Review-Token': token } },
    );
  }

  approveHumanReview(
    token: string,
    decisionNote = '',
  ): Observable<HumanReviewDecision> {
    return this.http.post<HumanReviewDecision>(
      `${this.baseUrl}/reviews/current/approve`,
      { decision_note: decisionNote || null },
      { headers: { 'X-Review-Token': token } },
    );
  }

  requestHumanReviewCorrection(
    token: string,
    decisionNote = '',
  ): Observable<HumanReviewDecision> {
    return this.http.post<HumanReviewDecision>(
      `${this.baseUrl}/reviews/current/request-correction`,
      { decision_note: decisionNote || null },
      { headers: { 'X-Review-Token': token } },
    );
  }

  submitCorrection(
    claimId: string,
    fieldName: string,
    value: string,
  ): Observable<{ claim_id: string; event_id: string; status: string }> {
    return this.http.post<{ claim_id: string; event_id: string; status: string }>(
      `${this.baseUrl}/claims/${claimId}/corrections`,
      { field_name: fieldName, value },
    );
  }
}
