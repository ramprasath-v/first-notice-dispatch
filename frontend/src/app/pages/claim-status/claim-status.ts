import { DatePipe, DecimalPipe, TitleCasePipe } from '@angular/common';
import { Component, DestroyRef, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute } from '@angular/router';
import {
  catchError,
  defer,
  EMPTY,
  exhaustMap,
  filter,
  finalize,
  map,
  merge,
  Observable,
  Subject,
  timer,
} from 'rxjs';
import { ClaimTimeline } from '../../components/claim-timeline/claim-timeline';
import { InspectionCard } from '../../components/inspection-card/inspection-card';
import {
  DocumentUploadRequest,
  MissingDocuments,
} from '../../components/missing-documents/missing-documents';
import { ClaimApiService } from '../../core/services/claim-api.service';
import {
  ClaimantEvidenceRequest,
  ClaimSummary,
  EnterTextRequestedAction,
  RequestedAction,
} from '../../models/claim';
import { ClaimEvent } from '../../models/claim-event';

export const STATUS_LABELS: Record<string, string> = {
  submitted: 'FirstNotice is reviewing your claim',
  new: 'FirstNotice is reviewing your claim',
  intake_processing: 'FirstNotice is reviewing your claim',
  intake_complete: 'Reviewing your evidence',
  review_processing: 'Reviewing your evidence',
  awaiting_documents: 'We need one more item from you',
  inspection_ready: 'Ready for inspection decision',
  inspection_pending: 'Inspection approved — scheduling',
  inspection_scheduled: 'Inspection scheduled',
  adjuster_notified: 'Inspection coordination complete',
  human_review_required: 'Adjuster review required',
  closed: 'Claim closed',
};

export const STATUS_DESCRIPTIONS: Record<string, string> = {
  submitted: 'We received your claim and will begin reviewing the submitted information.',
  new: 'We received your claim and will begin reviewing the submitted information.',
  intake_processing: 'FirstNotice is analyzing the information and evidence you submitted.',
  intake_complete:
    'Your evidence has been analyzed and the intake requirements are being reviewed.',
  review_processing: 'FirstNotice is checking the evidence and requirements needed to continue.',
  awaiting_documents: 'FirstNotice needs additional evidence before processing can continue.',
  inspection_ready:
    'FirstNotice completed your claim intake. Your evidence package is ready for an adjuster inspection decision.',
  inspection_pending:
    'Your claim has cleared intake review and FirstNotice is arranging the inspection.',
  inspection_scheduled: 'Your inspection has been scheduled.',
  adjuster_notified:
    'Your intake is complete and the claim information has been sent to the adjuster.',
  human_review_required:
    'FirstNotice identified information that requires an adjuster to review before processing can continue.',
  closed: 'This claim workflow is complete.',
};

export interface WorkflowStep {
  label: string;
  state: 'complete' | 'active' | 'upcoming';
  note: string;
}

export interface WorkflowHeartbeat {
  mode: 'active' | 'claimant' | 'adjuster' | 'scheduled' | 'complete';
  badge: string;
  title: string;
  detail: string;
  showProgress: boolean;
}

type RecheckKind = 'document' | 'correction';
type VoiceRecordingState = 'idle' | 'requesting' | 'recording' | 'recorded';

export function workflowSteps(status: string, rechecking = false): WorkflowStep[] {
  const analyzedActive = [
    'intake_processing',
    'intake_complete',
    'review_processing',
    'awaiting_documents',
    'human_review_required',
  ].includes(status);
  const inspectionReached = [
    'inspection_ready',
    'inspection_pending',
    'inspection_scheduled',
    'adjuster_notified',
    'closed',
  ].includes(status);
  const inspectionComplete = ['inspection_scheduled', 'adjuster_notified', 'closed'].includes(
    status,
  );
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
      state: inspectionComplete
        ? 'complete'
        : ['inspection_ready', 'inspection_pending'].includes(status)
          ? 'active'
          : 'upcoming',
      note:
        status === 'inspection_ready'
          ? 'Awaiting approval'
          : status === 'inspection_pending'
            ? 'Scheduling'
            : inspectionComplete
              ? 'Scheduled'
              : 'Up next',
    },
    {
      label: 'Adjuster',
      state: adjusterComplete
        ? 'complete'
        : ['inspection_scheduled'].includes(status)
          ? 'active'
          : 'upcoming',
      note: adjusterComplete
        ? 'Handoff complete'
        : status === 'inspection_scheduled'
          ? 'Preparing handoff'
          : 'Up next',
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
  imports: [DatePipe, DecimalPipe, TitleCasePipe, FormsModule, MissingDocuments, InspectionCard, ClaimTimeline],
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
  readonly recheckKind = signal<RecheckKind | null>(null);
  readonly pollingWarning = signal('');
  readonly pollInProgress = signal(false);
  readonly lastSuccessfulPollAt = signal<number | null>(null);
  readonly lastBusinessUpdateAt = signal<number | null>(null);
  readonly clock = signal(Date.now());
  readonly statusChangedUntil = signal(0);
  readonly voiceRecordingState = signal<VoiceRecordingState>('idle');
  readonly recordedVoice = signal<File | null>(null);
  private readonly refreshRequests = new Subject<void>();
  private refreshPending = false;
  private documentSubmittedAt: number | null = null;
  private statusAtUpload: string | null = null;
  private updatedAtAtUpload: string | null = null;
  private actionIdAtSubmission: string | null = null;
  private mediaRecorder: MediaRecorder | null = null;
  private microphoneStream: MediaStream | null = null;
  private voiceChunks: Blob[] = [];
  private discardRecordingOnStop = false;
  private voiceIdempotencyKey: string | null = null;
  correctionValue = '';

  constructor() {
    this.destroyRef.onDestroy(() => this.releaseMicrophone());
    timer(0, 1000)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(() => this.clock.set(Date.now()));
    merge(timer(0, 3000).pipe(map(() => false)), this.refreshRequests.pipe(map(() => true)))
      .pipe(
        filter((forceRefresh) => forceRefresh || this.shouldPoll()),
        exhaustMap(() =>
          this.claimId
            ? defer(() => {
                this.pollInProgress.set(true);
                return this.api.getClaim(this.claimId).pipe(
                  catchError(() => {
                    this.handleRefreshError();
                    return EMPTY;
                  }),
                  finalize(() => {
                    this.pollInProgress.set(false);
                    if (this.refreshPending) {
                      this.refreshPending = false;
                      queueMicrotask(() => this.refreshRequests.next());
                    }
                  }),
                );
              })
            : EMPTY,
        ),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe((claim) => this.acceptClaim(claim));
  }

  statusLabel(status: string): string {
    return STATUS_LABELS[status] || 'Claim update';
  }

  statusDescription(status: string): string {
    return STATUS_DESCRIPTIONS[status] || 'Your claim is moving through intake review.';
  }

  steps(status: string): WorkflowStep[] {
    return workflowSteps(status, this.rechecking());
  }

  uploadDocument(request: DocumentUploadRequest): void {
    this.uploadDocuments([request]);
  }

  uploadDocuments(requests: DocumentUploadRequest[]): void {
    if (!requests.length || this.uploading() || this.rechecking()) return;
    this.uploading.set(true);
    this.documentNotice.set('');
    this.error.set('');
    const allRequestedActions = requests.every(
      (request) => !!request.requestedActionId && !!request.idempotencyKey,
    );
    let upload: Observable<unknown>;
    if (requests.length > 1 && allRequestedActions) {
      upload = this.api.uploadDocuments(
          this.claimId,
          requests.map((request) => ({
            documentType: request.documentType,
            file: request.file,
            requestedActionId: request.requestedActionId!,
            idempotencyKey: request.idempotencyKey!,
          })),
        );
    } else if (requests[0].requestedActionId) {
      upload = this.api.uploadDocument(
        this.claimId, requests[0].documentType, requests[0].file,
        requests[0].requestedActionId, requests[0].idempotencyKey,
      );
    } else {
      upload = this.api.uploadDocument(
        this.claimId, requests[0].documentType, requests[0].file,
      );
    }
    upload.subscribe({
      next: () => {
        this.uploading.set(false);
        this.documentNotice.set('Document received. Rechecking your claim…');
        this.rechecking.set(true);
        this.recheckKind.set('document');
        this.pollingWarning.set('');
        this.documentSubmittedAt = Date.now();
        this.statusAtUpload = this.claim()?.status ?? 'awaiting_documents';
        this.updatedAtAtUpload = this.claim()?.updated_at ?? null;
        this.actionIdAtSubmission = null;
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
    const uploadActions = actions.filter(
      (action): action is Extract<RequestedAction, { action_type: 'upload_document' }> =>
        action.action_type === 'upload_document',
    );
    if (uploadActions.length) return uploadActions.map((action) => ({
      document_type: action.document_type,
      label: action.replaces_document_id ? 'Replacement evidence' : 'Requested evidence',
      instruction: action.instruction,
      satisfies_requirements: [],
      replacement_required: !!action.replaces_document_id,
      requested_action_id: action.action_id,
    }));
    if (actions.length) return [];
    return requestedEvidence.slice(0, 1);
  }

  textAction(actions: RequestedAction[] = []): EnterTextRequestedAction | null {
    const current = actions[0];
    return current?.action_type === 'enter_text' ? current : null;
  }

  workflowHeartbeat(status: string): WorkflowHeartbeat {
    if (this.rechecking()) {
      return this.recheckKind() === 'correction'
        ? {
            mode: 'active',
            badge: 'Live',
            title: 'Reviewing your response',
            detail: 'FirstNotice received your information and is continuing your claim.',
            showProgress: true,
          }
        : {
            mode: 'active',
            badge: 'Live',
            title: 'Reviewing your new evidence',
            detail: 'FirstNotice received your upload and is re-checking your claim.',
            showProgress: true,
          };
    }
    if (['submitted', 'new', 'intake_processing', 'intake_complete'].includes(status)) {
      return {
        mode: 'active',
        badge: 'Live',
        title: 'FirstNotice is reviewing your claim',
        detail:
          'Reviewing your police report and submitted evidence. This page updates automatically.',
        showProgress: true,
      };
    }
    if (status === 'review_processing') {
      return {
        mode: 'active',
        badge: 'Live',
        title: 'Reviewing your evidence',
        detail:
          'FirstNotice is checking the police report and submitted evidence. This page updates automatically.',
        showProgress: true,
      };
    }
    if (status === 'awaiting_documents') {
      return {
        mode: 'claimant',
        badge: 'Action needed',
        title: 'Waiting for information from you',
        detail: 'FirstNotice needs one item to continue processing your claim.',
        showProgress: false,
      };
    }
    if (status === 'human_review_required') {
      return {
        mode: 'adjuster',
        badge: 'Waiting for adjuster',
        title: 'Additional review is underway',
        detail: this.claim()?.manual_handling
          ? 'Your claim requires additional review by an adjuster. No action is required from you at this time.'
          : 'An adjuster is reviewing the evidence package. No action is needed from you.',
        showProgress: false,
      };
    }
    if (status === 'inspection_ready') {
      return {
        mode: 'adjuster',
        badge: 'Waiting for adjuster',
        title: 'Your intake is complete',
        detail:
          'An adjuster is reviewing the evidence package before authorizing inspection. No action is needed from you.',
        showProgress: false,
      };
    }
    if (status === 'inspection_pending') {
      return {
        mode: 'active',
        badge: 'Live',
        title: 'Inspection approved — scheduling',
        detail:
          'FirstNotice is preparing your inspection details. This page updates automatically.',
        showProgress: true,
      };
    }
    if (status === 'inspection_scheduled') {
      return {
        mode: 'scheduled',
        badge: 'Inspection scheduled',
        title: 'Your inspection is scheduled',
        detail: 'The appointment details are shown below.',
        showProgress: false,
      };
    }
    return {
      mode: 'complete',
      badge: 'Complete',
      title: 'Inspection coordination complete',
      detail: 'Your inspection details have been prepared and shared.',
      showProgress: false,
    };
  }

  relativeTime(timestamp: number | null): string {
    if (timestamp === null) return 'waiting for first update';
    const seconds = Math.max(0, Math.floor((this.clock() - timestamp) / 1000));
    if (seconds < 2) return 'just now';
    if (seconds < 60) return `${seconds} sec ago`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes} min ago`;
  }

  pollMessage(status: string): string {
    if (this.pollInProgress()) {
      return status === 'inspection_ready' || status === 'human_review_required'
        ? 'Checking for decision…'
        : 'Checking for updates…';
    }
    const last = this.lastSuccessfulPollAt();
    return last !== null && this.clock() - last < 1500 ? 'Up to date' : 'Updates automatic';
  }

  refreshNow(): void {
    if (this.pollInProgress()) {
      this.refreshPending = true;
      return;
    }
    this.refreshRequests.next();
  }

  submitCorrection(fieldName: string): void {
    const value = this.correctionValue.trim();
    if (this.uploading() || this.rechecking()) return;
    if (!value) {
      this.error.set(
        fieldName === 'incident_date'
          ? 'Please select the incident date.'
          : 'Please enter the requested information.',
      );
      return;
    }
    if (fieldName === 'incident_date' && !this.isValidIncidentDate(value)) {
      this.error.set('Please select a valid incident date that is not in the future.');
      return;
    }
    this.uploading.set(true);
    this.error.set('');
    this.api.submitCorrection(this.claimId, fieldName, value).subscribe({
      next: () => {
        this.uploading.set(false);
        this.correctionValue = '';
        this.documentNotice.set('Correction received. Rechecking your claim…');
        this.rechecking.set(true);
        this.recheckKind.set('correction');
        this.statusAtUpload = this.claim()?.status ?? 'awaiting_documents';
        this.updatedAtAtUpload = this.claim()?.updated_at ?? null;
        this.actionIdAtSubmission =
          this.claim()?.requested_actions?.find(
            (action) => action.action_type === 'enter_text' && action.field_name === fieldName,
          )?.action_id ?? null;
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

  async startVoiceRecording(): Promise<void> {
    if (
      typeof navigator === 'undefined' ||
      !navigator.mediaDevices?.getUserMedia ||
      typeof MediaRecorder === 'undefined'
    ) {
      this.error.set(
        'Voice recording is not supported in this browser. Please use a supported browser.',
      );
      return;
    }
    this.discardVoiceRecording();
    this.error.set('');
    this.voiceRecordingState.set('requesting');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this.microphoneStream = stream;
      const mimeType = this.preferredVoiceMimeType();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      this.mediaRecorder = recorder;
      this.voiceChunks = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) this.voiceChunks.push(event.data);
      };
      recorder.onstop = () => this.finishVoiceRecording(recorder.mimeType || mimeType);
      recorder.start();
      this.voiceRecordingState.set('recording');
    } catch {
      this.releaseMicrophone();
      this.voiceRecordingState.set('idle');
      this.error.set(
        'Microphone access was not available. Allow microphone access and try again.',
      );
    }
  }

  stopVoiceRecording(): void {
    if (this.mediaRecorder?.state === 'recording') this.mediaRecorder.stop();
  }

  discardVoiceRecording(): void {
    this.discardRecordingOnStop = true;
    if (this.mediaRecorder?.state === 'recording') {
      this.mediaRecorder.stop();
    } else {
      this.releaseMicrophone();
      this.resetVoiceRecording();
    }
  }

  submitVoiceCorrection(action: EnterTextRequestedAction): void {
    const file = this.recordedVoice();
    if (!file || this.uploading() || this.rechecking()) {
      if (!file) this.error.set('Please record a voice response before submitting.');
      return;
    }
    this.uploading.set(true);
    this.error.set('');
    const idempotencyKey = this.voiceIdempotencyKey ?? crypto.randomUUID();
    this.voiceIdempotencyKey = idempotencyKey;
    this.api.submitVoiceIncidentCorrection(
      this.claimId,
      action.action_id,
      file,
      idempotencyKey,
    ).subscribe({
      next: () => {
        this.uploading.set(false);
        this.resetVoiceRecording();
        this.documentNotice.set('Voice response received. Rechecking your claim…');
        this.rechecking.set(true);
        this.recheckKind.set('correction');
        this.statusAtUpload = this.claim()?.status ?? 'awaiting_documents';
        this.updatedAtAtUpload = this.claim()?.updated_at ?? null;
        this.actionIdAtSubmission = action.action_id;
        this.documentSubmittedAt = Date.now();
        this.refreshTimeline();
        this.refreshNow();
      },
      error: () => {
        this.uploading.set(false);
        this.error.set(
          'We could not use that recording. Please re-record your answer.',
        );
      },
    });
  }

  correctionSubmitDisabled(action: EnterTextRequestedAction): boolean {
    return this.uploading() || this.rechecking()
      || (action.field_name !== 'incident_date' && !this.correctionValue.trim());
  }

  private isValidIncidentDate(value: string): boolean {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const parsed = new Date(`${value}T00:00:00Z`);
    return !Number.isNaN(parsed.getTime())
      && parsed.toISOString().slice(0, 10) === value
      && value <= new Date().toISOString().slice(0, 10);
  }

  private preferredVoiceMimeType(): string {
    return ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'].find(
      (type) => MediaRecorder.isTypeSupported?.(type),
    ) ?? '';
  }

  private finishVoiceRecording(mimeType: string): void {
    this.releaseMicrophone();
    if (this.discardRecordingOnStop) {
      this.resetVoiceRecording();
      return;
    }
    const blob = new Blob(this.voiceChunks, { type: mimeType || 'audio/webm' });
    this.voiceChunks = [];
    this.mediaRecorder = null;
    if (!blob.size) {
      this.voiceRecordingState.set('idle');
      this.error.set('No audio was captured. Please try recording again.');
      return;
    }
    const extension = blob.type.includes('mp4') ? 'm4a' : 'webm';
    this.recordedVoice.set(
      new File([blob], `incident-voice-${Date.now()}.${extension}`, { type: blob.type }),
    );
    this.voiceIdempotencyKey = crypto.randomUUID();
    this.voiceRecordingState.set('recorded');
  }

  private releaseMicrophone(): void {
    this.microphoneStream?.getTracks().forEach((track) => track.stop());
    this.microphoneStream = null;
  }

  private resetVoiceRecording(): void {
    this.releaseMicrophone();
    this.mediaRecorder = null;
    this.voiceChunks = [];
    this.recordedVoice.set(null);
    this.voiceIdempotencyKey = null;
    this.discardRecordingOnStop = false;
    this.voiceRecordingState.set('idle');
  }

  private shouldPoll(): boolean {
    return shouldPollStatus(this.claim()?.status, this.rechecking());
  }

  private acceptClaim(claim: ClaimSummary): void {
    const previousStatus = this.claim()?.status;
    const now = Date.now();
    this.claim.set(claim);
    this.lastSuccessfulPollAt.set(now);
    const businessTimestamp = Date.parse(claim.updated_at);
    this.lastBusinessUpdateAt.set(Number.isNaN(businessTimestamp) ? now : businessTimestamp);
    if (previousStatus && previousStatus !== claim.status) this.statusChangedUntil.set(now + 900);
    this.loading.set(false);
    this.error.set('');
    if (!previousStatus || previousStatus !== claim.status) this.refreshTimeline();
    const statusChangedAfterSubmission = claim.status !== this.statusAtUpload;
    const claimUpdatedAfterSubmission =
      this.updatedAtAtUpload !== null && claim.updated_at !== this.updatedAtAtUpload;
    const submittedActionStillPending =
      this.actionIdAtSubmission !== null &&
      (claim.requested_actions ?? []).some(
        (action) => action.action_id === this.actionIdAtSubmission,
      );
    const correctionResolved =
      this.recheckKind() === 'correction' &&
      (statusChangedAfterSubmission || !submittedActionStillPending);
    const documentRecheckResolved =
      this.recheckKind() === 'document' &&
      (statusChangedAfterSubmission || claimUpdatedAfterSubmission);
    if (this.rechecking() && (correctionResolved || documentRecheckResolved)) {
      this.rechecking.set(false);
      this.recheckKind.set(null);
      this.documentNotice.set(
        claim.status === 'awaiting_documents'
          ? 'We still need a usable document. Please review the request and try again.'
          : '',
      );
      this.pollingWarning.set('');
      this.documentSubmittedAt = null;
      this.statusAtUpload = null;
      this.updatedAtAtUpload = null;
      this.actionIdAtSubmission = null;
    } else if (
      this.rechecking() &&
      this.recheckKind() === 'correction' &&
      submittedActionStillPending &&
      claimUpdatedAfterSubmission
    ) {
      // The correction POST may update the claim timestamp before the async
      // workflow replaces its action. Keep the local handoff active until the
      // authoritative status or requested-action identity changes.
      this.updatedAtAtUpload = claim.updated_at;
    } else if (
      this.rechecking() &&
      this.documentSubmittedAt !== null &&
      Date.now() - this.documentSubmittedAt >= DOCUMENT_RECHECK_TIMEOUT_MS
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
    this.error.set(
      this.claim()
        ? 'We could not refresh this claim just now. Retrying automatically.'
        : 'We could not load this claim. Check the claim ID and try again.',
    );
  }
}
