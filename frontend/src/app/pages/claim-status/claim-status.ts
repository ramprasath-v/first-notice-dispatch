import { DatePipe, TitleCasePipe } from '@angular/common';
import { Component, DestroyRef, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute } from '@angular/router';
import { catchError, EMPTY, exhaustMap, filter, map, merge, Subject, timer } from 'rxjs';
import { ClaimTimeline } from '../../components/claim-timeline/claim-timeline';
import { InspectionCard } from '../../components/inspection-card/inspection-card';
import { DocumentUploadRequest, MissingDocuments } from '../../components/missing-documents/missing-documents';
import { ClaimApiService } from '../../core/services/claim-api.service';
import { ClaimantEvidenceRequest, ClaimSummary, EnterTextRequestedAction, RequestedAction } from '../../models/claim';
import { ClaimEvent } from '../../models/claim-event';

export const STATUS_LABELS: Record<string, string> = {
  submitted: 'Claim received',
  new: 'Claim received',
  intake_processing: 'Analyzing your claim',
  intake_complete: 'Evidence analyzed',
  review_processing: 'Reviewing claim requirements',
  awaiting_documents: 'More information needed',
  inspection_pending: 'Preparing inspection',
  inspection_scheduled: 'Inspection scheduled',
  adjuster_notified: 'Ready for adjuster review',
  human_review_required: 'Additional review required',
  closed: 'Claim closed',
};

export const STATUS_DESCRIPTIONS: Record<string, string> = {
  submitted: 'We received your claim and will begin reviewing the submitted information.',
  new: 'We received your claim and will begin reviewing the submitted information.',
  intake_processing: 'FirstNotice is analyzing the information and evidence you submitted.',
  intake_complete: 'Your evidence has been analyzed and the intake requirements are being reviewed.',
  review_processing: 'FirstNotice is checking the evidence and requirements needed to continue.',
  awaiting_documents: 'FirstNotice needs additional evidence before processing can continue.',
  inspection_pending: 'Your claim has cleared intake review and FirstNotice is arranging the inspection.',
  inspection_scheduled: 'Your inspection has been scheduled.',
  adjuster_notified: 'Your intake is complete and the claim information has been sent to the adjuster.',
  human_review_required: 'FirstNotice identified information that requires an adjuster to review before processing can continue.',
  closed: 'This claim workflow is complete.',
};

export interface WorkflowStep {
  label: string;
  state: 'complete' | 'active' | 'upcoming';
  note: string;
}

export function workflowSteps(status: string, rechecking = false): WorkflowStep[] {
  const analyzedActive = [
    'intake_processing', 'intake_complete', 'review_processing',
    'awaiting_documents', 'human_review_required',
  ].includes(status);
  const inspectionReached = ['inspection_pending', 'inspection_scheduled', 'adjuster_notified', 'closed'].includes(status);
  const inspectionComplete = ['inspection_scheduled', 'adjuster_notified', 'closed'].includes(status);
  const adjusterReached = ['inspection_scheduled', 'adjuster_notified', 'closed'].includes(status);
  const adjusterComplete = ['adjuster_notified', 'closed'].includes(status);
  const analyzedNote = rechecking
    ? 'Rechecking evidence'
    : status === 'awaiting_documents'
      ? 'Waiting for information'
      : status === 'human_review_required'
        ? 'Human review required'
        : analyzedActive
          ? 'Review in progress'
          : 'Complete';

  return [
    {
      label: 'Received',
      state: ['submitted', 'new'].includes(status) ? 'active' : 'complete',
      note: ['submitted', 'new'].includes(status) ? 'Claim received' : 'Complete',
    },
    {
      label: 'Analyzed',
      state: analyzedActive ? 'active' : inspectionReached ? 'complete' : 'upcoming',
      note: analyzedNote,
    },
    {
      label: 'Inspection',
      state: inspectionComplete ? 'complete' : status === 'inspection_pending' ? 'active' : 'upcoming',
      note: status === 'inspection_pending' ? 'Being arranged' : inspectionComplete ? 'Complete' : 'Up next',
    },
    {
      label: 'Adjuster',
      state: adjusterComplete ? 'complete' : adjusterReached ? 'active' : 'upcoming',
      note: adjusterComplete ? 'Notified' : adjusterReached ? 'Preparing handoff' : 'Up next',
    },
  ];
}

const TERMINAL_STATES = new Set(['adjuster_notified', 'closed']);
export const DOCUMENT_RECHECK_TIMEOUT_MS = 60_000;

export function shouldPollStatus(status: string | undefined, resumePolling: boolean): boolean {
  if (!status) return true;
  if (TERMINAL_STATES.has(status)) return false;
  return status !== 'awaiting_documents' || resumePolling;
}

@Component({
  selector: 'app-claim-status-page',
  imports: [DatePipe, TitleCasePipe, FormsModule, MissingDocuments, InspectionCard, ClaimTimeline],
  templateUrl: './claim-status.html',
  styleUrl: './claim-status.scss',
})
export class ClaimStatusPage {
  private readonly api = inject(ClaimApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly destroyRef = inject(DestroyRef);
  readonly claimId = this.route.snapshot.paramMap.get('claimId') ?? '';
  readonly claim = signal<ClaimSummary | null>(null);
  readonly events = signal<ClaimEvent[]>([]);
  readonly loading = signal(true);
  readonly error = signal('');
  readonly uploading = signal(false);
  readonly documentNotice = signal('');
  readonly rechecking = signal(false);
  readonly pollingWarning = signal('');
  private readonly refreshRequests = new Subject<void>();
  private documentSubmittedAt: number | null = null;
  private statusAtUpload: string | null = null;
  private updatedAtAtUpload: string | null = null;
  correctionValue = '';

  constructor() {
    merge(
      timer(0, 3000).pipe(map(() => false)),
      this.refreshRequests.pipe(map(() => true)),
    ).pipe(
      filter((forceRefresh) => forceRefresh || this.shouldPoll()),
      exhaustMap(() => this.claimId
        ? this.api.getClaim(this.claimId).pipe(
          catchError(() => {
            this.handleRefreshError();
            return EMPTY;
          }),
        )
        : EMPTY),
      takeUntilDestroyed(this.destroyRef),
    ).subscribe((claim) => this.acceptClaim(claim));
  }

  statusLabel(status: string): string { return STATUS_LABELS[status] || 'Claim update'; }

  statusDescription(status: string): string {
    return STATUS_DESCRIPTIONS[status] || 'Your claim is moving through intake review.';
  }

  steps(status: string): WorkflowStep[] { return workflowSteps(status, this.rechecking()); }

  uploadDocument(request: DocumentUploadRequest): void {
    this.uploading.set(true);
    this.documentNotice.set('');
    this.error.set('');
    const upload = request.requestedActionId
      ? this.api.uploadDocument(
        this.claimId,
        request.documentType,
        request.file,
        request.requestedActionId,
        request.idempotencyKey,
      )
      : this.api.uploadDocument(this.claimId, request.documentType, request.file);
    upload.subscribe({
      next: () => {
        this.uploading.set(false);
        this.documentNotice.set('Document received. Rechecking your claim…');
        this.rechecking.set(true);
        this.pollingWarning.set('');
        this.documentSubmittedAt = Date.now();
        this.statusAtUpload = this.claim()?.status ?? 'awaiting_documents';
        this.updatedAtAtUpload = this.claim()?.updated_at ?? null;
        this.refreshTimeline();
        this.refreshNow();
      },
      error: () => {
        this.uploading.set(false);
        this.error.set('We could not upload that document. Please check the file and try again.');
      },
    });
  }

  evidenceRequests(
    requestedEvidence: ClaimantEvidenceRequest[],
    actions: RequestedAction[] = [],
  ): ClaimantEvidenceRequest[] {
    const currentAction = actions[0];
    if (currentAction?.action_type === 'upload_document') {
      return [{
        document_type: currentAction.document_type,
        label: 'Replacement evidence',
        instruction: currentAction.instruction,
        satisfies_requirements: [],
        replacement_required: true,
        requested_action_id: currentAction.action_id,
      }];
    }
    if (currentAction) return [];
    return requestedEvidence.slice(0, 1);
  }

  textAction(actions: RequestedAction[] = []): EnterTextRequestedAction | null {
    const current = actions[0];
    return current?.action_type === 'enter_text' ? current : null;
  }

  workflowIndicator(status: string): { active: boolean; title: string; detail: string } {
    if (this.rechecking() || ['new', 'intake_processing', 'review_processing', 'inspection_pending', 'inspection_scheduled'].includes(status)) {
      return { active: true, title: 'FirstNotice is working', detail: this.rechecking() ? 'Rechecking your evidence…' : 'Processing your claim…' };
    }
    if (status === 'awaiting_documents') {
      return { active: false, title: 'Waiting for your information', detail: 'Complete the action below when you are ready.' };
    }
    if (status === 'human_review_required') {
      return { active: false, title: 'Waiting for adjuster review', detail: 'An adjuster is reviewing the current evidence.' };
    }
    return { active: false, title: 'Current step complete', detail: 'Your latest claim status is shown above.' };
  }

  refreshNow(): void {
    this.refreshRequests.next();
  }

  submitCorrection(fieldName: string): void {
    const value = this.correctionValue.trim();
    if (!value || this.uploading()) return;
    this.uploading.set(true);
    this.error.set('');
    this.api.submitCorrection(this.claimId, fieldName, value).subscribe({
      next: () => {
        this.uploading.set(false);
        this.correctionValue = '';
        this.documentNotice.set('Correction received. Rechecking your claim…');
        this.rechecking.set(true);
        this.statusAtUpload = this.claim()?.status ?? 'awaiting_documents';
        this.updatedAtAtUpload = this.claim()?.updated_at ?? null;
        this.documentSubmittedAt = Date.now();
        this.refreshTimeline();
        this.refreshNow();
      },
      error: () => {
        this.uploading.set(false);
        this.error.set('We could not submit that correction. Please try again.');
      },
    });
  }

  private shouldPoll(): boolean {
    return shouldPollStatus(this.claim()?.status, this.rechecking());
  }

  private acceptClaim(claim: ClaimSummary): void {
    const previousStatus = this.claim()?.status;
    this.claim.set(claim);
    this.loading.set(false);
    this.error.set('');
    if (!previousStatus || previousStatus !== claim.status) this.refreshTimeline();
    if (
      this.rechecking()
      && (
        claim.status !== this.statusAtUpload
        || (this.updatedAtAtUpload !== null && claim.updated_at !== this.updatedAtAtUpload)
      )
    ) {
      this.rechecking.set(false);
      this.documentNotice.set(
        claim.status === 'awaiting_documents'
          ? 'We still need a usable document. Please review the request and try again.'
          : '',
      );
      this.pollingWarning.set('');
      this.documentSubmittedAt = null;
      this.statusAtUpload = null;
      this.updatedAtAtUpload = null;
    } else if (
      this.rechecking()
      && this.documentSubmittedAt !== null
      && Date.now() - this.documentSubmittedAt >= DOCUMENT_RECHECK_TIMEOUT_MS
    ) {
      this.pollingWarning.set(
        'Rechecking is taking longer than expected. We will keep refreshing automatically.',
      );
    }
  }

  private refreshTimeline(): void {
    this.api.getClaimEvents(this.claimId).subscribe({ next: (events) => this.events.set(events) });
  }

  private handleRefreshError(): void {
    this.loading.set(false);
    this.error.set(this.claim()
      ? 'We could not refresh this claim just now. Retrying automatically.'
      : 'We could not load this claim. Check the claim ID and try again.');
  }
}
