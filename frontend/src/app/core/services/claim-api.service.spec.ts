import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { ClaimApiService } from './claim-api.service';

describe('ClaimApiService', () => {
  let service: ClaimApiService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(ClaimApiService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('submits multipart evidence with the client idempotency key', () => {
    service.submitClaim({
      incidentDescription: 'Rear-ended',
      evidenceFiles: [
        new File(['photo'], 'damage.jpg', { type: 'image/jpeg' }),
        new File(['pdf'], 'report.pdf', { type: 'application/pdf' }),
        new File(['policy'], 'policy.png', { type: 'image/png' }),
      ],
    }, 'same-request-key').subscribe();

    const request = http.expectOne('http://localhost:8080/claims');
    expect(request.request.method).toBe('POST');
    expect(request.request.headers.get('X-Idempotency-Key')).toBe('same-request-key');
    expect(request.request.body).toBeInstanceOf(FormData);
    expect((request.request.body as FormData).getAll('files')).toHaveLength(3);
    request.flush({ claim_id: 'CLM-1', status: 'new', event_id: 'evt', message: 'ok' });
  });

  it('submits one unified evidence list without semantic buckets', () => {
    service.submitClaim({
      incidentDescription: 'Rear-ended',
      evidenceFiles: [new File(['report'], 'report.jpg', { type: 'image/jpeg' })],
    }, 'same-request-key').subscribe();

    const request = http.expectOne('http://localhost:8080/claims');
    expect((request.request.body as FormData).getAll('files')).toHaveLength(1);
    request.flush({ claim_id: 'CLM-1', status: 'new', event_id: 'evt', message: 'ok' });
  });

  it('loads claim state, events, and uploads a missing document', () => {
    service.getClaim('CLM-1').subscribe();
    http.expectOne('http://localhost:8080/claims/CLM-1').flush({});
    service.getClaimEvents('CLM-1').subscribe();
    http.expectOne('http://localhost:8080/claims/CLM-1/events').flush([]);
    service.uploadDocument('CLM-1', 'license_plate_photo', new File(['x'], 'plate.jpg')).subscribe();
    const upload = http.expectOne('http://localhost:8080/claims/CLM-1/documents');
    expect(upload.request.method).toBe('POST');
    expect(upload.request.body.get('document_type')).toBe('license_plate_photo');
    upload.flush({});
  });

  it('sends the server action ID and stable idempotency key for replacement evidence', () => {
    service.uploadDocument(
      'CLM-1',
      'damage_evidence',
      new File(['x'], 'correct-damage.jpg'),
      'ACT-REPLACE',
      'replacement-request-1',
    ).subscribe();

    const upload = http.expectOne('http://localhost:8080/claims/CLM-1/documents');
    expect(upload.request.headers.get('X-Idempotency-Key')).toBe('replacement-request-1');
    expect(upload.request.body.get('requested_action_id')).toBe('ACT-REPLACE');
    expect(upload.request.body.has('replaces_document_id')).toBe(false);
    upload.flush({});
  });

  it('uses token-scoped review endpoints', () => {
    service.getHumanReview('token/value').subscribe();
    const load = http.expectOne('http://localhost:8080/reviews/current');
    expect(load.request.headers.get('X-Review-Token')).toBe('token/value');
    load.flush({});

    service.approveHumanReview('token/value').subscribe();
    const approve = http.expectOne('http://localhost:8080/reviews/current/approve');
    expect(approve.request.method).toBe('POST');
    expect(approve.request.headers.get('X-Review-Token')).toBe('token/value');
    approve.flush({});

    service.requestHumanReviewCorrection('token/value', 'Confirm policy').subscribe();
    const correction = http.expectOne('http://localhost:8080/reviews/current/request-correction');
    expect(correction.request.body.decision_note).toBe('Confirm policy');
    expect(correction.request.body.correction_type).toBeUndefined();
    expect(correction.request.body.target_document_id).toBeUndefined();
    correction.flush({});
  });
});
