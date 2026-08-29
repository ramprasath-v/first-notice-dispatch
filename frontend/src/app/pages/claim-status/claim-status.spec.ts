import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute } from '@angular/router';
import { of, Subject, throwError } from 'rxjs';
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
  missing_documents: [{ type: 'vehicle_identity' }, { type: 'license_plate_photo' }],
  requested_evidence: [
    {
      document_type: 'license_plate_photo',
      label: 'License Plate Photo',
      instruction: "Please upload a clear photo of your vehicle's license plate.",
      satisfies_requirements: ['license_plate_photo', 'vehicle_identity'],
      replacement_required: false,
    },
  ],
});

describe('ClaimStatusPage', () => {
  const api = {
    getClaim: vi.fn(),
    getClaimEvents: vi.fn(),
    uploadDocument: vi.fn(),
    uploadDocuments: vi.fn(),
    submitCorrection: vi.fn(),
    submitVoiceIncidentCorrection: vi.fn(),
  };

  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    api.getClaim.mockReturnValue(of(awaitingClaim));
    api.getClaimEvents.mockReturnValue(of([]));
    api.uploadDocument.mockReturnValue(of({ status: 'received' }));
    api.uploadDocuments.mockReturnValue(of([{ status: 'received' }, { status: 'received' }]));
    api.submitCorrection.mockReturnValue(
      of({ claim_id: 'CLM-ABC12345', event_id: 'evt', status: 'received' }),
    );
    api.submitVoiceIncidentCorrection.mockReturnValue(
      of({ claim_id: 'CLM-ABC12345', event_id: 'voice-evt', status: 'received' }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  async function create(): Promise<ComponentFixture<ClaimStatusPage>> {
    await TestBed.configureTestingModule({
      imports: [ClaimStatusPage],
      providers: [
        { provide: ClaimApiService, useValue: api },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: { get: () => 'CLM-ABC12345' } } },
        },
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
    expect(fixture.nativeElement.textContent).toContain(
      "clear photo of your vehicle's license plate",
    );
    expect(fixture.nativeElement.querySelectorAll('.missing-item')).toHaveLength(1);
    expect(fixture.nativeElement.textContent).not.toContain('Why we need this');
    expect(fixture.nativeElement.textContent).toContain('What to do');
  });

  it('renders the backend-grounded title, reason, and upload instruction', async () => {
    api.getClaim.mockReturnValue(
      of({
        ...awaitingClaim,
        action_display: {
          title: 'Vehicle identity not verified',
          explanation: 'The submitted photo does not show enough information to verify the insured vehicle.',
        },
      }),
    );

    const fixture = await create();

    expect(fixture.nativeElement.textContent).toContain('Vehicle identity not verified');
    expect(fixture.nativeElement.textContent).toContain('Why we need this');
    expect(fixture.nativeElement.textContent).toContain('does not show enough information');
    expect(fixture.nativeElement.textContent).toContain('What to do');
    expect(fixture.nativeElement.querySelectorAll('.missing-item')).toHaveLength(1);
  });

  it('renders enter_text as the existing text correction UI', async () => {
    api.getClaim.mockReturnValue(
      of(
        claim('awaiting_documents', {
          action_display: {
            title: "Policy information doesn't match",
            explanation: 'The submitted policy information contains different policy numbers.',
          },
          requested_actions: [
            {
              action_type: 'enter_text',
              action_id: 'ACT-TEXT',
              review_id: 'HRV-1',
              field_name: 'policy_number',
              instruction: 'Please confirm your policy number.',
            },
          ],
        }),
      ),
    );
    const fixture = await create();
    expect(fixture.nativeElement.querySelector('.correction-card input')).not.toBeNull();
    expect(fixture.nativeElement.querySelector('app-missing-documents')).toBeNull();
    expect(fixture.nativeElement.querySelector('input[type=file]')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Please confirm your policy number');
    expect(fixture.nativeElement.textContent).toContain("Policy information doesn't match");
    expect(fixture.nativeElement.textContent).toContain('Why we need this');
    expect(fixture.nativeElement.textContent).toContain('What to do');
  });

  it('renders missing incident date as voice-only remediation', async () => {
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      missing_documents: [{ type: 'incident_date' }],
      requested_actions: [{
        action_type: 'enter_text',
        action_id: 'ACT-DATE',
        review_id: 'AUTONOMOUS-DATE',
        field_name: 'incident_date',
        instruction: 'Please provide the incident date to continue.',
      }],
    })));

    const fixture = await create();

    expect(fixture.nativeElement.textContent).toContain(
      'Tell us about the accident',
    );
    expect(fixture.nativeElement.textContent).toContain(
      'Tell us when the accident happened and briefly what happened. You can also mention any injuries or other important details.',
    );
    expect(fixture.nativeElement.textContent).toContain('Record voice note');
    expect(fixture.nativeElement.querySelector('input[type=date]')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('enter the date manually');
    expect(fixture.nativeElement.textContent).not.toContain('Continue');
    expect(fixture.nativeElement.querySelector('input[type=file]')).toBeNull();
    expect(fixture.nativeElement.querySelector('app-missing-documents')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('incident_date');
  });

  it('renders a context-only gap as voice remediation without a file upload', async () => {
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      missing_documents: [{ type: 'incident_description' }],
      requested_evidence: [],
      requested_actions: [{
        action_type: 'enter_text',
        action_id: 'ACT-CONTEXT',
        review_id: 'AUTONOMOUS-CONTEXT',
        field_name: 'incident_description',
        instruction: 'Tell us when and what happened.',
      }],
    })));

    const fixture = await create();

    expect(fixture.nativeElement.textContent).toContain('Tell us about the accident');
    expect(fixture.nativeElement.textContent).toContain('Record voice note');
    expect(fixture.nativeElement.querySelector('app-missing-documents')).toBeNull();
    expect(fixture.nativeElement.querySelector('input[type=file]')).toBeNull();
    expect(fixture.nativeElement.querySelector('.correction-card input')).toBeNull();
  });

  it('renders combined missing date and context as one voice action', async () => {
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      missing_documents: [{ type: 'incident_date' }, { type: 'incident_description' }],
      requested_evidence: [],
      requested_actions: [{
        action_type: 'enter_text',
        action_id: 'ACT-INCIDENT',
        review_id: 'AUTONOMOUS-INCIDENT',
        field_name: 'incident_information',
        instruction: 'Tell us when and what happened.',
      }],
    })));

    const fixture = await create();

    expect(fixture.nativeElement.querySelectorAll('.correction-card').length).toBe(1);
    expect(fixture.nativeElement.textContent).toContain('Record voice note');
    expect(fixture.nativeElement.querySelector('app-missing-documents')).toBeNull();
  });

  it('records, stops, and submits voice through the incident-date action', async () => {
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      requested_actions: [{
        action_type: 'enter_text', action_id: 'ACT-DATE', review_id: 'AUTONOMOUS-DATE',
        field_name: 'incident_date', instruction: 'Please provide the incident date.',
      }],
    })));
    class FakeRecorder {
      static isTypeSupported = () => true;
      state = 'inactive';
      mimeType = 'audio/webm';
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(_stream: MediaStream, _options?: MediaRecorderOptions) {}
      start(): void { this.state = 'recording'; }
      stop(): void {
        this.state = 'inactive';
        this.ondataavailable?.({ data: new Blob(['voice'], { type: 'audio/webm' }) });
        this.onstop?.();
      }
    }
    const stop = vi.fn();
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop }] }) },
    });
    vi.stubGlobal('MediaRecorder', FakeRecorder);
    const fixture = await create();

    await fixture.componentInstance.startVoiceRecording();
    expect(fixture.componentInstance.voiceRecordingState()).toBe('recording');
    fixture.componentInstance.stopVoiceRecording();
    expect(fixture.componentInstance.voiceRecordingState()).toBe('recorded');
    const action = fixture.componentInstance.textAction(
      fixture.componentInstance.claim()?.requested_actions,
    )!;
    fixture.componentInstance.submitVoiceCorrection(action);

    expect(api.submitVoiceIncidentCorrection).toHaveBeenCalledWith(
      'CLM-ABC12345',
      'ACT-DATE',
      expect.objectContaining({ type: 'audio/webm' }),
      expect.any(String),
    );
    expect(fixture.componentInstance.rechecking()).toBe(true);
    expect(stop).toHaveBeenCalled();
  });

  it('uses the existing document processing UI after voice upload acceptance', async () => {
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      requested_actions: [{
        action_type: 'enter_text', action_id: 'ACT-DATE', review_id: 'AUTONOMOUS-DATE',
        field_name: 'incident_date', instruction: 'Please provide the incident date.',
      }],
    })));
    const fixture = await create();
    fixture.componentInstance.recordedVoice.set(
      new File(['voice'], 'voice.webm', { type: 'audio/webm' }),
    );
    fixture.componentInstance.voiceRecordingState.set('recorded');
    const action = fixture.componentInstance.textAction(
      fixture.componentInstance.claim()?.requested_actions,
    )!;

    fixture.componentInstance.submitVoiceCorrection(action);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain(
      'Voice note received. Rechecking your claim',
    );
    expect(fixture.nativeElement.textContent).toContain('Reviewing your new evidence');
    expect(fixture.nativeElement.textContent).not.toContain('Voice response received');
    expect(fixture.nativeElement.textContent).not.toContain('Submit voice note');
    expect(fixture.nativeElement.textContent).not.toContain('Re-record');
    expect(fixture.nativeElement.textContent).not.toContain('Discard');
  });

  it('prevents duplicate voice submission while upload acceptance is pending', async () => {
    const accepted = new Subject<{ claim_id: string; event_id: string; status: string }>();
    api.submitVoiceIncidentCorrection.mockReturnValue(accepted);
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      requested_actions: [{
        action_type: 'enter_text', action_id: 'ACT-DATE', review_id: 'AUTONOMOUS-DATE',
        field_name: 'incident_date', instruction: 'Please provide the incident date.',
      }],
    })));
    const fixture = await create();
    fixture.componentInstance.recordedVoice.set(
      new File(['voice'], 'voice.webm', { type: 'audio/webm' }),
    );
    fixture.componentInstance.voiceRecordingState.set('recorded');
    const action = fixture.componentInstance.textAction(
      fixture.componentInstance.claim()?.requested_actions,
    )!;

    fixture.componentInstance.submitVoiceCorrection(action);
    fixture.componentInstance.submitVoiceCorrection(action);

    expect(api.submitVoiceIncidentCorrection).toHaveBeenCalledOnce();
    expect(fixture.componentInstance.uploading()).toBe(true);
  });

  it('moves from accepted voice processing through normal status polling', async () => {
    const pending = claim('awaiting_documents', {
      requested_actions: [{
        action_type: 'enter_text', action_id: 'ACT-DATE', review_id: 'AUTONOMOUS-DATE',
        field_name: 'incident_date', instruction: 'Please provide the incident date.',
      }],
      voice_correction_processing: {
        requested_action_id: 'ACT-DATE', status: 'accepted',
      },
      updated_at: '2026-08-07T12:00:01Z',
    });
    api.getClaim
      .mockReturnValueOnce(of(claim('awaiting_documents', {
        requested_actions: pending.requested_actions,
      })))
      .mockReturnValueOnce(of(pending))
      .mockReturnValueOnce(of(claim('human_review_required')));
    const fixture = await create();
    fixture.componentInstance.recordedVoice.set(
      new File(['voice'], 'voice.webm', { type: 'audio/webm' }),
    );
    fixture.componentInstance.voiceRecordingState.set('recorded');
    fixture.componentInstance.submitVoiceCorrection(
      fixture.componentInstance.textAction(
        fixture.componentInstance.claim()?.requested_actions,
      )!,
    );
    fixture.detectChanges();
    expect(fixture.componentInstance.rechecking()).toBe(true);

    vi.advanceTimersByTime(3000);
    fixture.detectChanges();

    expect(fixture.componentInstance.claim()?.status).toBe('human_review_required');
    expect(fixture.componentInstance.rechecking()).toBe(false);
  });

  it('restores voice remediation when async processing rejects the recording', async () => {
    const action = {
      action_type: 'enter_text' as const,
      action_id: 'ACT-DATE', review_id: 'AUTONOMOUS-DATE',
      field_name: 'incident_date', instruction: 'Please provide the incident date.',
    };
    api.getClaim
      .mockReturnValueOnce(of(claim('awaiting_documents', { requested_actions: [action] })))
      .mockReturnValueOnce(of(claim('awaiting_documents', {
        requested_actions: [action],
        updated_at: '2026-08-07T12:00:01Z',
        voice_correction_processing: {
          requested_action_id: 'ACT-DATE',
          status: 'unusable',
          message: 'We could not use that recording. Please re-record your answer.',
        },
      })));
    const fixture = await create();
    fixture.componentInstance.recordedVoice.set(
      new File(['voice'], 'voice.webm', { type: 'audio/webm' }),
    );
    fixture.componentInstance.voiceRecordingState.set('recorded');

    fixture.componentInstance.submitVoiceCorrection(action);
    fixture.detectChanges();

    expect(fixture.componentInstance.rechecking()).toBe(false);
    expect(fixture.nativeElement.textContent).toContain('Record voice note');
    expect(fixture.nativeElement.textContent).toContain(
      'We could not use that recording. Please re-record your answer.',
    );
  });

  it('allows discard and re-record without exposing manual date entry', async () => {
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      requested_actions: [{
        action_type: 'enter_text', action_id: 'ACT-DATE', review_id: 'AUTONOMOUS-DATE',
        field_name: 'incident_date', instruction: 'Please provide the incident date.',
      }],
    })));
    const fixture = await create();
    fixture.componentInstance.recordedVoice.set(
      new File(['voice'], 'voice.webm', { type: 'audio/webm' }),
    );
    fixture.componentInstance.voiceRecordingState.set('recorded');
    fixture.componentInstance.discardVoiceRecording();
    fixture.detectChanges();

    expect(fixture.componentInstance.recordedVoice()).toBeNull();
    expect(fixture.componentInstance.voiceRecordingState()).toBe('idle');
    expect(fixture.nativeElement.querySelector('input[type=date]')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Record voice note');
  });

  it('handles microphone permission failure with voice-only guidance', async () => {
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      requested_actions: [{
        action_type: 'enter_text', action_id: 'ACT-DATE', review_id: 'AUTONOMOUS-DATE',
        field_name: 'incident_date', instruction: 'Please provide the incident date.',
      }],
    })));
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn().mockRejectedValue(new Error('denied')) },
    });
    vi.stubGlobal('MediaRecorder', class {});
    const fixture = await create();

    await fixture.componentInstance.startVoiceRecording();
    fixture.detectChanges();

    expect(fixture.componentInstance.error()).toContain('Microphone access');
    expect(fixture.componentInstance.error()).not.toContain('manually');
    expect(fixture.nativeElement.querySelector('input[type=date]')).toBeNull();
  });

  it('reports unsupported recording without suggesting manual date entry', async () => {
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      requested_actions: [{
        action_type: 'enter_text', action_id: 'ACT-DATE', review_id: 'AUTONOMOUS-DATE',
        field_name: 'incident_date', instruction: 'Please provide the incident date.',
      }],
    })));
    vi.stubGlobal('MediaRecorder', undefined);
    const fixture = await create();

    await fixture.componentInstance.startVoiceRecording();
    fixture.detectChanges();

    expect(fixture.componentInstance.error()).toContain('not supported');
    expect(fixture.componentInstance.error()).not.toContain('manually');
    expect(fixture.nativeElement.querySelector('input[type=date]')).toBeNull();
  });

  it('keeps the recording available when voice upload fails', async () => {
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      requested_actions: [{
        action_type: 'enter_text', action_id: 'ACT-DATE', review_id: 'AUTONOMOUS-DATE',
        field_name: 'incident_date', instruction: 'Please provide the incident date.',
      }],
    })));
    api.submitVoiceIncidentCorrection.mockReturnValue(
      throwError(() => new Error('upload failed')),
    );
    const fixture = await create();
    const recording = new File(['voice'], 'voice.webm', { type: 'audio/webm' });
    fixture.componentInstance.recordedVoice.set(recording);
    fixture.componentInstance.voiceRecordingState.set('recorded');
    const action = fixture.componentInstance.textAction(
      fixture.componentInstance.claim()?.requested_actions,
    )!;

    fixture.componentInstance.submitVoiceCorrection(action);

    expect(fixture.componentInstance.recordedVoice()).toBe(recording);
    expect(fixture.componentInstance.rechecking()).toBe(false);
    expect(fixture.componentInstance.error()).toBe(
      'We could not use that recording. Please re-record your answer.',
    );
  });

  it('submits a valid incident date through the correction API', async () => {
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      requested_actions: [{
        action_type: 'enter_text', action_id: 'ACT-DATE', review_id: 'AUTONOMOUS-DATE',
        field_name: 'incident_date', instruction: 'Please provide the incident date.',
      }],
    })));
    const fixture = await create();
    fixture.componentInstance.correctionValue = '2026-08-07';

    fixture.componentInstance.submitCorrection('incident_date');

    expect(api.submitCorrection).toHaveBeenCalledWith(
      'CLM-ABC12345', 'incident_date', '2026-08-07',
    );
    expect(api.uploadDocument).not.toHaveBeenCalled();
  });

  it('rejects an invalid or empty incident date without calling the API', async () => {
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      requested_actions: [{
        action_type: 'enter_text', action_id: 'ACT-DATE', review_id: 'AUTONOMOUS-DATE',
        field_name: 'incident_date', instruction: 'Please provide the incident date.',
      }],
    })));
    const fixture = await create();

    fixture.componentInstance.submitCorrection('incident_date');
    expect(api.submitCorrection).not.toHaveBeenCalled();
    expect(fixture.componentInstance.error()).toContain('select the incident date');

    fixture.componentInstance.correctionValue = 'invalid';
    fixture.componentInstance.submitCorrection('incident_date');
    expect(api.submitCorrection).not.toHaveBeenCalled();
    expect(fixture.componentInstance.error()).toContain('valid incident date');
  });

  it('does not render a date picker when incident date was already extracted', async () => {
    api.getClaim.mockReturnValue(of(claim('inspection_ready')));

    const fixture = await create();

    expect(fixture.nativeElement.querySelector('input[type=date]')).toBeNull();
  });

  it('renders upload_document as one file picker without a correction text box', async () => {
    api.getClaim.mockReturnValue(
      of(
        claim('awaiting_documents', {
          requested_actions: [
            {
              action_type: 'upload_document',
              action_id: 'ACT-REPLACE',
              review_id: 'HRV-1',
              document_type: 'damage_evidence',
              instruction: 'Please upload the correct damage photo.',
              replaces_document_id: 'DOC-OLD',
            },
          ],
        }),
      ),
    );
    const fixture = await create();
    expect(fixture.nativeElement.querySelectorAll('input[type=file]')).toHaveLength(1);
    expect(fixture.nativeElement.querySelector('.correction-card input')).toBeNull();
  });

  it('renders mismatch-specific Flow 4 copy from the grounded backend display', async () => {
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      action_display: {
        title: "New evidence doesn't match",
        explanation: 'The uploaded vehicle does not match the insured vehicle on the policy.',
        instruction: 'Please upload a clear photo of the insured vehicle with a readable license plate.',
      },
      requested_actions: [{
        action_type: 'upload_document',
        action_id: 'ACT-FLOW-4',
        review_id: 'AUTONOMOUS-FLOW-4',
        document_type: 'license_plate_photo',
        instruction: 'Please upload a clear rear or license-plate photo of the involved vehicle.',
        replaces_document_id: 'DOC-WRONG',
      }],
    })));

    const fixture = await create();

    expect(fixture.nativeElement.textContent).toContain(
      "New evidence doesn't match",
    );
    expect(fixture.nativeElement.textContent).toContain(
      'The uploaded vehicle does not match the insured vehicle on the policy.',
    );
    expect(fixture.nativeElement.textContent).toContain(
      'Please upload a clear photo of the insured vehicle with a readable license plate.',
    );
    expect(fixture.nativeElement.textContent).not.toContain(
      'Please upload a clear rear or license-plate photo of the involved vehicle.',
    );
    expect(fixture.nativeElement.querySelector('input[type=file]')).not.toBeNull();
  });

  it('keeps initial Flow 4 identity remediation generic', async () => {
    api.getClaim.mockReturnValue(of(claim('awaiting_documents', {
      action_display: {
        title: 'Vehicle identity not verified',
        explanation: 'The submitted photo does not show enough information to verify the insured vehicle.',
      },
      requested_actions: [{
        action_type: 'upload_document',
        action_id: 'ACT-INITIAL-FLOW-4',
        review_id: 'AUTONOMOUS-INITIAL-FLOW-4',
        document_type: 'license_plate_photo',
        instruction: 'Please upload a clear vehicle photo with a readable license plate.',
        replaces_document_id: 'DOC-INITIAL',
      }],
    })));

    const fixture = await create();

    expect(fixture.nativeElement.textContent).toContain('Vehicle identity not verified');
    expect(fixture.nativeElement.textContent).toContain(
      'Please upload a clear vehicle photo with a readable license plate.',
    );
    expect(fixture.nativeElement.textContent).not.toContain(
      "New evidence doesn't match",
    );
  });

  it('prioritizes a human-review action over ordinary missing evidence', async () => {
    api.getClaim.mockReturnValue(
      of(
        claim('awaiting_documents', {
          requested_evidence: awaitingClaim.requested_evidence,
          requested_actions: [
            {
              action_type: 'upload_document',
              action_id: 'ACT-REPLACE',
              review_id: 'HRV-1',
              document_type: 'damage_evidence',
              instruction: 'Please upload the correct damage photo.',
              replaces_document_id: 'DOC-OLD',
            },
          ],
        }),
      ),
    );
    const fixture = await create();
    expect(fixture.nativeElement.querySelectorAll('input[type=file]')).toHaveLength(1);
    expect(fixture.nativeElement.textContent).toContain('Please upload the correct damage photo');
    expect(fixture.nativeElement.textContent).not.toContain("vehicle's license plate");
  });

  it('shows only the next ordinary evidence action and advances after reevaluation', async () => {
    const twoRequests = [
      ...awaitingClaim.requested_evidence,
      {
        document_type: 'police_report',
        label: 'Police report',
        instruction: 'Please upload the police report.',
        satisfies_requirements: ['police_report'],
        replacement_required: false,
      },
    ];
    api.getClaim
      .mockReturnValueOnce(of(claim('awaiting_documents', { requested_evidence: twoRequests })))
      .mockReturnValueOnce(
        of(claim('awaiting_documents', { requested_evidence: twoRequests.slice(1) })),
      );
    const fixture = await create();
    expect(fixture.nativeElement.querySelectorAll('input[type=file]')).toHaveLength(1);
    expect(fixture.nativeElement.textContent).toContain("vehicle's license plate");

    fixture.componentInstance.refreshNow();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('input[type=file]')).toHaveLength(1);
    expect(fixture.nativeElement.textContent).toContain('Please upload the police report');
  });

  it('shows waiting indicators without a spinner for claimant and adjuster waits', async () => {
    const fixture = await create();
    expect(fixture.nativeElement.textContent).toContain('Waiting for information from you');
    expect(fixture.nativeElement.querySelector('.indeterminate-progress')).toBeNull();
    expect(fixture.nativeElement.querySelector('.poll-row')).not.toBeNull();

    api.getClaim.mockReturnValue(of(claim('inspection_ready')));
    fixture.componentInstance.refreshNow();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Waiting for adjuster');
    expect(fixture.nativeElement.textContent).toContain('Your intake is complete');
    expect(fixture.nativeElement.querySelector('.indeterminate-progress')).toBeNull();
    expect(fixture.nativeElement.querySelector('.poll-row')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('Up to date');
    expect(fixture.nativeElement.textContent).not.toContain('Last checked:');
    expect(fixture.nativeElement.textContent).not.toContain('Workflow update:');
  });

  it('hides polling metadata while human review waits for an adjuster', async () => {
    api.getClaim.mockReturnValue(of(claim('human_review_required')));
    const fixture = await create();

    expect(fixture.nativeElement.textContent).toContain('Waiting for adjuster');
    expect(fixture.nativeElement.textContent).toContain('Your intake is complete');
    expect(fixture.nativeElement.textContent).toContain(
      'An adjuster is reviewing the evidence package before authorizing inspection. No action is needed from you.',
    );
    expect(fixture.nativeElement.querySelector('.poll-row')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('Up to date');
    expect(fixture.nativeElement.textContent).not.toContain('Updates automatic');
    expect(fixture.nativeElement.textContent).not.toContain('Last checked:');
    expect(fixture.nativeElement.textContent).not.toContain('Workflow update:');
  });

  it('sends requested_action_id and resumes polling for replacement upload', async () => {
    api.getClaim.mockReturnValue(
      of(
        claim('awaiting_documents', {
          requested_actions: [
            {
              action_type: 'upload_document',
              action_id: 'ACT-REPLACE',
              review_id: 'HRV-1',
              document_type: 'damage_evidence',
              instruction: 'Upload the correct damage photo.',
              replaces_document_id: 'DOC-OLD',
            },
          ],
        }),
      ),
    );
    const fixture = await create();
    const callsBeforeUpload = api.getClaim.mock.calls.length;
    fixture.componentInstance.uploadDocument({
      documentType: 'damage_evidence',
      file: new File(['x'], 'correct.jpg'),
      requestedActionId: 'ACT-REPLACE',
      idempotencyKey: 'stable-upload-key',
    });

    expect(api.uploadDocument).toHaveBeenCalledWith(
      'CLM-ABC12345',
      'damage_evidence',
      expect.any(File),
      'ACT-REPLACE',
      'stable-upload-key',
    );
    expect(api.getClaim.mock.calls.length).toBe(callsBeforeUpload + 1);
    expect(fixture.componentInstance.rechecking()).toBe(true);
  });

  it('submits multiple requested evidence files in one claimant action', async () => {
    const fixture = await create();

    fixture.componentInstance.uploadDocuments([
      {
        documentType: 'policy_document',
        file: new File(['policy'], 'policy.pdf'),
        requestedActionId: 'ACT-POLICY',
        idempotencyKey: 'policy-upload-key',
      },
      {
        documentType: 'police_report',
        file: new File(['report'], 'report.pdf'),
        requestedActionId: 'ACT-REPORT',
        idempotencyKey: 'report-upload-key',
      },
    ]);

    expect(api.uploadDocuments).toHaveBeenCalledWith('CLM-ABC12345', [
      {
        documentType: 'policy_document', file: expect.any(File),
        requestedActionId: 'ACT-POLICY', idempotencyKey: 'policy-upload-key',
      },
      {
        documentType: 'police_report', file: expect.any(File),
        requestedActionId: 'ACT-REPORT', idempotencyKey: 'report-upload-key',
      },
    ]);
    expect(fixture.componentInstance.rechecking()).toBe(true);
  });

  it('immediately replaces a successful evidence action with the processing handoff', async () => {
    const fixture = await create();
    upload(fixture);

    expect(fixture.nativeElement.querySelector('app-missing-documents')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Reviewing your new evidence');
    expect(fixture.nativeElement.textContent).toContain(
      'received your upload and is re-checking your claim',
    );
    expect(
      fixture.nativeElement.querySelector('.workflow-heartbeat .indeterminate-progress'),
    ).not.toBeNull();

    upload(fixture);
    expect(api.uploadDocument).toHaveBeenCalledOnce();
  });

  it('leaves the evidence action available when upload fails', async () => {
    api.uploadDocument.mockReturnValue(throwError(() => new Error('upload failed')));
    const fixture = await create();
    upload(fixture);

    expect(fixture.componentInstance.rechecking()).toBe(false);
    expect(fixture.nativeElement.querySelector('app-missing-documents')).not.toBeNull();
    expect(fixture.nativeElement.textContent).toContain('could not upload that document');
  });

  it('immediately hides a successful text correction and reuses claim refresh', async () => {
    const textClaim = claim('awaiting_documents', {
      requested_actions: [
        {
          action_type: 'enter_text',
          action_id: 'ACT-TEXT',
          review_id: 'HRV-1',
          field_name: 'policy_number',
          instruction: 'Please enter policy number.',
        },
      ],
    });
    api.getClaim.mockReturnValue(of(textClaim));
    const fixture = await create();
    const callsBeforeCorrection = api.getClaim.mock.calls.length;
    fixture.componentInstance.correctionValue = 'POL-1001';

    fixture.componentInstance.submitCorrection('policy_number');
    fixture.detectChanges();

    expect(api.submitCorrection).toHaveBeenCalledWith('CLM-ABC12345', 'policy_number', 'POL-1001');
    expect(api.getClaim.mock.calls.length).toBe(callsBeforeCorrection + 1);
    expect(fixture.nativeElement.querySelector('.correction-card')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Reviewing your response');
    expect(fixture.nativeElement.textContent).toContain(
      'received your information and is continuing your claim',
    );

    fixture.componentInstance.correctionValue = 'POL-1001';
    fixture.componentInstance.submitCorrection('policy_number');
    expect(api.submitCorrection).toHaveBeenCalledOnce();
  });

  it('keeps the correction handoff through a stale timestamp-only GET, then accepts the fresh state', async () => {
    const textClaim = claim('awaiting_documents', {
      updated_at: '2026-08-07T12:00:00Z',
      requested_actions: [
        {
          action_type: 'enter_text',
          action_id: 'ACT-TEXT',
          review_id: 'HRV-1',
          field_name: 'policy_number',
          instruction: 'Please enter policy number.',
        },
      ],
    });
    const timestampOnlyUpdate = { ...textClaim, updated_at: '2026-08-07T12:00:10Z' };
    const nextState = claim('inspection_ready', { updated_at: '2026-08-07T12:00:20Z' });
    api.getClaim
      .mockReturnValue(of(nextState))
      .mockReturnValueOnce(of(textClaim))
      .mockReturnValueOnce(of(timestampOnlyUpdate));
    const fixture = await create();
    fixture.componentInstance.correctionValue = 'POL-1001';

    fixture.componentInstance.submitCorrection('policy_number');
    fixture.detectChanges();

    expect(api.getClaim).toHaveBeenCalledTimes(2);
    expect(fixture.componentInstance.rechecking()).toBe(true);
    expect(fixture.nativeElement.querySelector('.correction-card')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Reviewing your response');

    vi.advanceTimersByTime(3000);
    fixture.detectChanges();

    expect(api.getClaim).toHaveBeenCalledTimes(3);
    expect(fixture.componentInstance.rechecking()).toBe(false);
    expect(fixture.nativeElement.textContent).toContain('Ready for inspection decision');
    expect(fixture.nativeElement.textContent).not.toContain('Reviewing your response');
    expect(fixture.nativeElement.querySelector('.correction-card')).toBeNull();

    vi.advanceTimersByTime(3000);
    expect(api.getClaim).toHaveBeenCalledTimes(4);
  });

  it('leaves a failed text correction available for retry', async () => {
    api.getClaim.mockReturnValue(
      of(
        claim('awaiting_documents', {
          requested_actions: [
            {
              action_type: 'enter_text',
              action_id: 'ACT-TEXT',
              review_id: 'HRV-1',
              field_name: 'policy_number',
              instruction: 'Please enter policy number.',
            },
          ],
        }),
      ),
    );
    api.submitCorrection.mockReturnValue(throwError(() => new Error('correction failed')));
    const fixture = await create();
    fixture.componentInstance.correctionValue = 'POL-1001';

    fixture.componentInstance.submitCorrection('policy_number');
    fixture.detectChanges();

    expect(fixture.componentInstance.rechecking()).toBe(false);
    expect(fixture.nativeElement.querySelector('.correction-card')).not.toBeNull();
    expect(fixture.nativeElement.textContent).toContain('could not submit that correction');
    expect(
      (fixture.nativeElement.querySelector('.correction-card button') as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  it('queues one forced refresh when correction succeeds during an active poll', async () => {
    const textClaim = claim('awaiting_documents', {
      requested_actions: [
        {
          action_type: 'enter_text',
          action_id: 'ACT-TEXT',
          review_id: 'HRV-1',
          field_name: 'policy_number',
          instruction: 'Please enter policy number.',
        },
      ],
    });
    const activePoll = new Subject<ClaimSummary>();
    api.getClaim
      .mockReturnValueOnce(of(textClaim))
      .mockReturnValueOnce(activePoll)
      .mockReturnValueOnce(of(textClaim));
    const fixture = await create();
    fixture.componentInstance.refreshNow();
    expect(fixture.componentInstance.pollInProgress()).toBe(true);
    fixture.componentInstance.correctionValue = 'POL-1001';

    fixture.componentInstance.submitCorrection('policy_number');
    expect(api.getClaim).toHaveBeenCalledTimes(2);
    activePoll.complete();
    await Promise.resolve();

    expect(api.getClaim).toHaveBeenCalledTimes(3);
    fixture.destroy();
  });

  it('makes an unusable replacement actionable again after same-state review completion', async () => {
    const replacementClaim = claim('awaiting_documents', {
      updated_at: '2026-08-07T12:00:00Z',
      requested_actions: [
        {
          action_type: 'upload_document',
          action_id: 'ACT-REPLACE',
          review_id: 'HRV-1',
          document_type: 'damage_evidence',
          instruction: 'Upload the correct damage photo.',
          replaces_document_id: 'DOC-OLD',
        },
      ],
    });
    api.getClaim
      .mockReturnValueOnce(of(replacementClaim))
      .mockReturnValueOnce(of(replacementClaim))
      .mockReturnValueOnce(of({ ...replacementClaim, updated_at: '2026-08-07T12:01:00Z' }));
    const fixture = await create();
    fixture.componentInstance.uploadDocument({
      documentType: 'damage_evidence',
      file: new File(['x'], 'blurry.jpg'),
      requestedActionId: 'ACT-REPLACE',
      idempotencyKey: 'stable-upload-key',
    });
    vi.advanceTimersByTime(3000);
    fixture.detectChanges();

    expect(fixture.componentInstance.rechecking()).toBe(false);
    expect(fixture.nativeElement.textContent).toContain('still need a usable document');
    expect(fixture.nativeElement.querySelector('.file-button').classList.contains('disabled')).toBe(
      false,
    );
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
    expect(fixture.nativeElement.textContent).toContain(
      'An adjuster is reviewing the evidence package',
    );
  });

  it('shows Inspection active while an appointment is being prepared', async () => {
    api.getClaim.mockReturnValue(of(claim('inspection_pending')));
    const fixture = await create();
    const steps = fixture.nativeElement.querySelectorAll('.step');
    expect(steps[1].dataset.state).toBe('complete');
    expect(steps[2].dataset.state).toBe('active');
    expect(steps[2].textContent).toContain('Scheduling');
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
    expect(fixture.nativeElement.textContent).toContain(
      'inspection details have been prepared and shared',
    );
  });

  it('resumes polling immediately after document upload', async () => {
    const fixture = await create();
    const callsBeforeUpload = api.getClaim.mock.calls.length;

    upload(fixture);

    expect(api.uploadDocument).toHaveBeenCalledWith(
      'CLM-ABC12345',
      'license_plate_photo',
      expect.any(File),
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
    expect(fixture.nativeElement.textContent).toContain('Reviewing your new evidence');
    expect(fixture.nativeElement.querySelector('app-missing-documents')).toBeNull();

    vi.advanceTimersByTime(3000);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Inspection approved — scheduling');
    expect(fixture.nativeElement.textContent).not.toContain('Reviewing your new evidence');
    expect(fixture.componentInstance.rechecking()).toBe(false);
  });

  it('updates inspection_pending to inspection_scheduled automatically', async () => {
    api.getClaim.mockReturnValueOnce(of(claim('inspection_pending'))).mockReturnValueOnce(
      of(
        claim('inspection_scheduled', {
          inspection: {
            appointment_id: 'APT-1',
            inspection_type: 'virtual',
            status: 'scheduled',
            scheduled_start: '2026-08-08T17:00:00Z',
            scheduled_end: '2026-08-08T18:00:00Z',
            location_type: 'virtual',
            location_details: 'Secure video call',
          },
        }),
      ),
    );
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

    expect(fixture.nativeElement.textContent).toContain('Inspection coordination complete');
    const callsAtTerminalState = api.getClaim.mock.calls.length;
    vi.advanceTimersByTime(9000);
    expect(api.getClaim).toHaveBeenCalledTimes(callsAtTerminalState);
  });

  it('continues polling while rendering human_review_required', async () => {
    api.getClaim.mockReturnValue(of(claim('human_review_required')));
    const fixture = await create();
    expect(fixture.nativeElement.textContent).toContain('Adjuster review required');
    const callsBeforePoll = api.getClaim.mock.calls.length;
    vi.advanceTimersByTime(3000);
    expect(api.getClaim).toHaveBeenCalledTimes(callsBeforePoll + 1);
  });

  it('shows that durable manual handling requires no claimant action', async () => {
    api.getClaim.mockReturnValue(of(claim('human_review_required', {
      manual_handling: true,
    })));
    const fixture = await create();

    expect(fixture.nativeElement.textContent).toContain(
      'Your claim requires additional review by an adjuster. No action is required from you at this time.',
    );
  });

  it('updates an externally approved human review to inspection_pending', async () => {
    api.getClaim
      .mockReturnValueOnce(of(claim('human_review_required')))
      .mockReturnValueOnce(of(claim('inspection_pending')));
    const fixture = await create();

    vi.advanceTimersByTime(3000);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Inspection approved — scheduling');
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
      .mockReturnValueOnce(
        of([
          {
            action: 'human_review_requested',
            actor: 'firstnoticeai',
            timestamp: '2026-08-07T12:00:00Z',
            details: {},
            correlation_id: 'review-1',
          },
        ]),
      )
      .mockReturnValueOnce(
        of([
          {
            action: 'human_review_approved',
            actor: 'adjuster',
            timestamp: '2026-08-07T12:01:00Z',
            details: { review_id: 'REV-1' },
            correlation_id: 'review-1',
          },
          {
            action: 'human_review_resumed',
            actor: 'firstnoticeai',
            timestamp: '2026-08-07T12:01:01Z',
            details: { review_id: 'REV-1' },
            correlation_id: 'review-1',
          },
        ]),
      );
    const fixture = await create();
    expect(fixture.nativeElement.textContent).toContain('Paused the claim for human review');

    vi.advanceTimersByTime(3000);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Human approval received');
    expect(fixture.nativeElement.textContent).toContain(
      'Automatically resumed the claim after human review',
    );
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
    expect(workflowSteps('awaiting_documents')[1]).toMatchObject({
      state: 'active',
      note: 'Waiting for information',
    });
    expect(workflowSteps('awaiting_documents', true)[1].note).toBe('Rechecking evidence');
    expect(workflowSteps('human_review_required')[1].note).toBe('Human review required');
    expect(workflowSteps('inspection_pending')[2].state).toBe('active');
    expect(workflowSteps('inspection_scheduled')[3].state).toBe('active');
    expect(workflowSteps('adjuster_notified').every((step) => step.state === 'complete')).toBe(
      true,
    );
  });

  it('renders active processing with a live heartbeat and indeterminate bar', async () => {
    api.getClaim.mockReturnValue(of(claim('review_processing')));
    const fixture = await create();
    const heartbeat = fixture.nativeElement.querySelector('.workflow-heartbeat');
    expect(heartbeat.dataset.mode).toBe('active');
    expect(heartbeat.textContent).toContain('Live');
    expect(heartbeat.textContent).toContain('Reviewing your evidence');
    expect(heartbeat.querySelector('.indeterminate-progress')).not.toBeNull();
  });

  it('shows poll-in-progress and then successful up-to-date feedback', async () => {
    const response = new Subject<ClaimSummary>();
    api.getClaim.mockReturnValue(response);
    await TestBed.configureTestingModule({
      imports: [ClaimStatusPage],
      providers: [
        { provide: ClaimApiService, useValue: api },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: { get: () => 'CLM-ABC12345' } } },
        },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(ClaimStatusPage);
    vi.advanceTimersByTime(0);
    expect(fixture.componentInstance.pollInProgress()).toBe(true);
    response.next(claim('review_processing'));
    response.complete();
    fixture.detectChanges();
    expect(fixture.componentInstance.pollInProgress()).toBe(false);
    expect(fixture.nativeElement.textContent).toContain('Up to date');
  });

  it('updates relative last-checked time locally without another request', async () => {
    const fixture = await create();
    const calls = api.getClaim.mock.calls.length;
    fixture.componentInstance.lastSuccessfulPollAt.set(Date.now() - 12_000);
    fixture.componentInstance.clock.set(Date.now());
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('12 sec ago');
    expect(api.getClaim).toHaveBeenCalledTimes(calls);
  });

  it('renders scheduled and completed heartbeat states without processing animation', async () => {
    api.getClaim.mockReturnValueOnce(of(claim('inspection_scheduled')));
    const scheduled = await create();
    expect(scheduled.nativeElement.querySelector('.workflow-heartbeat').dataset.mode).toBe(
      'scheduled',
    );
    expect(scheduled.nativeElement.querySelector('.indeterminate-progress')).toBeNull();
    scheduled.destroy();

    api.getClaim.mockReturnValueOnce(of(claim('adjuster_notified')));
    const completed = TestBed.createComponent(ClaimStatusPage);
    vi.advanceTimersByTime(0);
    completed.detectChanges();
    expect(completed.nativeElement.querySelector('.workflow-heartbeat').dataset.mode).toBe(
      'complete',
    );
    expect(completed.nativeElement.textContent).toContain('Inspection coordination complete');
  });

  it('keeps exactly one action panel ahead of the consolidated activity card', async () => {
    const fixture = await create();
    const actions = fixture.nativeElement.querySelectorAll(
      'app-missing-documents, .correction-card',
    );
    expect(actions).toHaveLength(1);
    expect(fixture.nativeElement.querySelectorAll('app-claim-timeline')).toHaveLength(1);
    expect(
      actions[0].compareDocumentPosition(
        fixture.nativeElement.querySelector('app-claim-timeline'),
      ) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
