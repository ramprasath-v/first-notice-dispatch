import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute } from '@angular/router';
import { of, throwError } from 'rxjs';
import { ClaimApiService } from '../../core/services/claim-api.service';
import { ClaimSummary } from '../../models/claim';
import {
  ClaimStatusPage,
  DOCUMENT_RECHECK_TIMEOUT_MS,
  shouldPollStatus,
  workflowSteps,
} from './claim-status';

const claim = (status: string, extra: Partial<ClaimSummary> = {}): ClaimSummary => ({
  claim_id: 'CLM-ABC12345',
  status,
  intake_priority: 'routine',
  missing_documents: [],
  requested_evidence: [],
  updated_at: '2026-08-07T12:00:00Z',
  ...extra,
});

const awaitingClaim = claim('awaiting_documents', {
  missing_documents: [
    { type: 'vehicle_identity' },
    { type: 'license_plate_photo' },
  ],
  requested_evidence: [{
    document_type: 'license_plate_photo',
    label: 'License Plate Photo',
    instruction: "Please upload a clear photo of your vehicle's license plate.",
    satisfies_requirements: ['license_plate_photo', 'vehicle_identity'],
    replacement_required: false,
  }],
});

describe('ClaimStatusPage', () => {
  const api = {
    getClaim: vi.fn(), getClaimEvents: vi.fn(), uploadDocument: vi.fn(),
  };

  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    api.getClaim.mockReturnValue(of(awaitingClaim));
    api.getClaimEvents.mockReturnValue(of([]));
    api.uploadDocument.mockReturnValue(of({ status: 'received' }));
  });

  afterEach(() => vi.useRealTimers());

  async function create(): Promise<ComponentFixture<ClaimStatusPage>> {
    await TestBed.configureTestingModule({
      imports: [ClaimStatusPage],
      providers: [
        { provide: ClaimApiService, useValue: api },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: { get: () => 'CLM-ABC12345' } } } },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(ClaimStatusPage);
    vi.advanceTimersByTime(0);
    fixture.detectChanges();
    return fixture;
  }

  function upload(fixture: ComponentFixture<ClaimStatusPage>): void {
    fixture.componentInstance.uploadDocument({
      documentType: 'license_plate_photo',
      file: new File(['x'], 'plate.jpg'),
    });
    fixture.detectChanges();
  }

  it('renders awaiting_documents with one upload action for equivalent identity gaps', async () => {
    const fixture = await create();
    expect(fixture.nativeElement.textContent).toContain('More information needed');
    expect(fixture.nativeElement.textContent).toContain("clear photo of your vehicle's license plate");
    expect(fixture.nativeElement.querySelectorAll('.missing-item')).toHaveLength(1);
  });

  it('shows Analyzed as the current waiting stage for awaiting_documents', async () => {
    const fixture = await create();
    const steps = fixture.nativeElement.querySelectorAll('.step');
    expect(steps[0].dataset.state).toBe('complete');
    expect(steps[1].dataset.state).toBe('active');
    expect(steps[1].textContent).toContain('Waiting for information');
    expect(steps[2].dataset.state).toBe('upcoming');
  });

  it('shows a human-review pause in the existing Analyzed stage', async () => {
    api.getClaim.mockReturnValue(of(claim('human_review_required')));
    const fixture = await create();
    const steps = fixture.nativeElement.querySelectorAll('.step');
    expect(steps[1].dataset.state).toBe('active');
    expect(steps[1].textContent).toContain('Human review required');
    expect(fixture.nativeElement.textContent).toContain('requires an adjuster to review');
  });

  it('shows Inspection active while an appointment is being prepared', async () => {
    api.getClaim.mockReturnValue(of(claim('inspection_pending')));
    const fixture = await create();
    const steps = fixture.nativeElement.querySelectorAll('.step');
    expect(steps[1].dataset.state).toBe('complete');
    expect(steps[2].dataset.state).toBe('active');
    expect(steps[2].textContent).toContain('Being arranged');
  });

  it('shows Inspection complete and Adjuster active once inspection is scheduled', async () => {
    api.getClaim.mockReturnValue(of(claim('inspection_scheduled')));
    const fixture = await create();
    const steps = fixture.nativeElement.querySelectorAll('.step');
    expect(steps[2].dataset.state).toBe('complete');
    expect(steps[3].dataset.state).toBe('active');
    expect(steps[3].textContent).toContain('Preparing handoff');
  });

  it('renders every major stage complete for adjuster_notified', async () => {
    api.getClaim.mockReturnValue(of(claim('adjuster_notified')));
    const fixture = await create();
    const steps = [...fixture.nativeElement.querySelectorAll('.step')] as HTMLElement[];
    expect(steps.every((step) => step.dataset['state'] === 'complete')).toBe(true);
    expect(fixture.nativeElement.textContent).toContain('claim information has been sent to the adjuster');
  });

  it('resumes polling immediately after document upload', async () => {
    const fixture = await create();
    const callsBeforeUpload = api.getClaim.mock.calls.length;

    upload(fixture);

    expect(api.uploadDocument).toHaveBeenCalledWith(
      'CLM-ABC12345', 'license_plate_photo', expect.any(File),
    );
    expect(api.getClaim.mock.calls.length).toBe(callsBeforeUpload + 1);
    expect(fixture.nativeElement.textContent).toContain('Document received. Rechecking your claim');
    expect(fixture.componentInstance.rechecking()).toBe(true);
  });

  it('updates awaiting_documents to inspection_pending automatically', async () => {
    api.getClaim
      .mockReturnValueOnce(of(awaitingClaim))
      .mockReturnValueOnce(of(awaitingClaim))
      .mockReturnValueOnce(of(claim('inspection_pending')));
    const fixture = await create();
    upload(fixture);

    vi.advanceTimersByTime(3000);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Preparing inspection');
    expect(fixture.componentInstance.rechecking()).toBe(false);
  });

  it('updates inspection_pending to inspection_scheduled automatically', async () => {
    api.getClaim
      .mockReturnValueOnce(of(claim('inspection_pending')))
      .mockReturnValueOnce(of(claim('inspection_scheduled', {
        inspection: {
          appointment_id: 'APT-1', inspection_type: 'virtual', status: 'scheduled',
          scheduled_start: '2026-08-08T17:00:00Z', scheduled_end: '2026-08-08T18:00:00Z',
          location_type: 'virtual', location_details: 'Secure video call',
        },
      })));
    const fixture = await create();

    vi.advanceTimersByTime(3000);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Inspection scheduled');
    expect(fixture.nativeElement.textContent).toContain('Secure video call');
  });

  it('updates inspection_scheduled to adjuster_notified automatically', async () => {
    api.getClaim
      .mockReturnValueOnce(of(claim('inspection_scheduled')))
      .mockReturnValueOnce(of(claim('adjuster_notified')));
    const fixture = await create();

    vi.advanceTimersByTime(3000);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Ready for adjuster review');
    const callsAtTerminalState = api.getClaim.mock.calls.length;
    vi.advanceTimersByTime(9000);
    expect(api.getClaim).toHaveBeenCalledTimes(callsAtTerminalState);
  });

  it('continues polling while rendering human_review_required', async () => {
    api.getClaim.mockReturnValue(of(claim('human_review_required')));
    const fixture = await create();
    expect(fixture.nativeElement.textContent).toContain('Additional review required');
    const callsBeforePoll = api.getClaim.mock.calls.length;
    vi.advanceTimersByTime(3000);
    expect(api.getClaim).toHaveBeenCalledTimes(callsBeforePoll + 1);
  });

  it('updates an externally approved human review to inspection_pending', async () => {
    api.getClaim
      .mockReturnValueOnce(of(claim('human_review_required')))
      .mockReturnValueOnce(of(claim('inspection_pending')));
    const fixture = await create();

    vi.advanceTimersByTime(3000);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Preparing inspection');
    expect(api.getClaimEvents).toHaveBeenCalledTimes(2);
  });

  it('refreshes the timeline whenever status changes', async () => {
    api.getClaim
      .mockReturnValueOnce(of(claim('inspection_pending')))
      .mockReturnValueOnce(of(claim('inspection_scheduled')));
    await create();
    expect(api.getClaimEvents).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(3000);

    expect(api.getClaimEvents).toHaveBeenCalledTimes(2);
  });

  it('refreshes Agent Activity after a polled status transition', async () => {
    api.getClaim
      .mockReturnValueOnce(of(claim('human_review_required')))
      .mockReturnValueOnce(of(claim('inspection_pending')));
    api.getClaimEvents
      .mockReturnValueOnce(of([{
        action: 'human_review_requested', actor: 'firstnoticeai', timestamp: '2026-08-07T12:00:00Z',
        details: {}, correlation_id: 'review-1',
      }]))
      .mockReturnValueOnce(of([{
        action: 'human_review_approved', actor: 'adjuster', timestamp: '2026-08-07T12:01:00Z',
        details: { review_id: 'REV-1' }, correlation_id: 'review-1',
      }, {
        action: 'human_review_resumed', actor: 'firstnoticeai', timestamp: '2026-08-07T12:01:01Z',
        details: { review_id: 'REV-1' }, correlation_id: 'review-1',
      }]));
    const fixture = await create();
    expect(fixture.nativeElement.textContent).toContain('Paused the claim for human review');

    vi.advanceTimersByTime(3000);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Human approval received');
    expect(fixture.nativeElement.textContent).toContain('Automatically resumed the claim after human review');
    expect(api.getClaimEvents).toHaveBeenCalledTimes(2);
  });

  it('keeps one polling subscription after repeated uploads and destroys it on navigation', async () => {
    const fixture = await create();
    upload(fixture);
    upload(fixture);
    const callsBeforeTick = api.getClaim.mock.calls.length;

    vi.advanceTimersByTime(3000);
    expect(api.getClaim.mock.calls.length).toBe(callsBeforeTick + 1);

    fixture.destroy();
    const callsAtDestroy = api.getClaim.mock.calls.length;
    vi.advanceTimersByTime(6000);
    expect(api.getClaim).toHaveBeenCalledTimes(callsAtDestroy);
  });

  it('creates only one polling subscription after navigation re-entry', async () => {
    api.getClaim.mockReturnValue(of(claim('human_review_required')));
    const first = await create();
    first.destroy();
    const callsAfterExit = api.getClaim.mock.calls.length;

    const second = TestBed.createComponent(ClaimStatusPage);
    vi.advanceTimersByTime(0);
    second.detectChanges();
    expect(api.getClaim.mock.calls.length).toBe(callsAfterExit + 1);

    const callsBeforeTick = api.getClaim.mock.calls.length;
    vi.advanceTimersByTime(3000);
    expect(api.getClaim.mock.calls.length).toBe(callsBeforeTick + 1);
    second.destroy();
  });

  it('survives a transient refresh error and retries on the next poll', async () => {
    api.getClaim
      .mockReturnValueOnce(of(claim('inspection_pending')))
      .mockReturnValueOnce(throwError(() => new Error('temporary')))
      .mockReturnValueOnce(of(claim('inspection_scheduled')));
    const fixture = await create();

    vi.advanceTimersByTime(3000);
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Retrying automatically');
    vi.advanceTimersByTime(3000);
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Inspection scheduled');
  });

  it('warns without stopping when document recheck takes too long', async () => {
    const fixture = await create();
    upload(fixture);

    vi.advanceTimersByTime(DOCUMENT_RECHECK_TIMEOUT_MS);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('taking longer than expected');
    expect(fixture.componentInstance.rechecking()).toBe(true);
  });

  it('pauses untouched awaiting_documents and stops only for terminal states', () => {
    expect(shouldPollStatus('new', false)).toBe(true);
    expect(shouldPollStatus('intake_processing', false)).toBe(true);
    expect(shouldPollStatus('review_processing', false)).toBe(true);
    expect(shouldPollStatus('awaiting_documents', false)).toBe(false);
    expect(shouldPollStatus('awaiting_documents', true)).toBe(true);
    expect(shouldPollStatus('inspection_pending', false)).toBe(true);
    expect(shouldPollStatus('inspection_scheduled', false)).toBe(true);
    expect(shouldPollStatus('adjuster_notified', true)).toBe(false);
    expect(shouldPollStatus('human_review_required', false)).toBe(true);
    expect(shouldPollStatus('closed', true)).toBe(false);
  });

  it('maps each major state without exposing raw status values', () => {
    expect(workflowSteps('new')[0].state).toBe('active');
    expect(workflowSteps('awaiting_documents')[1]).toMatchObject({ state: 'active', note: 'Waiting for information' });
    expect(workflowSteps('awaiting_documents', true)[1].note).toBe('Rechecking evidence');
    expect(workflowSteps('human_review_required')[1].note).toBe('Human review required');
    expect(workflowSteps('inspection_pending')[2].state).toBe('active');
    expect(workflowSteps('inspection_scheduled')[3].state).toBe('active');
    expect(workflowSteps('adjuster_notified').every((step) => step.state === 'complete')).toBe(true);
  });
});
