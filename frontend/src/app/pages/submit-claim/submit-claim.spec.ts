import { HttpResponse } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { of, Subject, throwError } from 'rxjs';
import { ClaimApiService } from '../../core/services/claim-api.service';
import { SubmitClaim } from './submit-claim';

describe('SubmitClaim', () => {
  let fixture: ComponentFixture<SubmitClaim>;
  let component: SubmitClaim;
  const api = { submitClaim: vi.fn() };
  const router = { navigate: vi.fn() };

  beforeEach(async () => {
    vi.clearAllMocks();
    await TestBed.configureTestingModule({
      imports: [SubmitClaim],
      providers: [
        { provide: ClaimApiService, useValue: api },
        { provide: Router, useValue: router },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(SubmitClaim);
    component = fixture.componentInstance;
  });

  it('rejects submission without a damage photo', () => {
    component.form.controls.incidentDescription.setValue('Rear-ended');
    component.submit();
    fixture.detectChanges();
    expect(api.submitClaim).not.toHaveBeenCalled();
    expect(fixture.nativeElement.textContent).toContain('Add at least one damage photo');
  });

  it('rejects submission without a police report', () => {
  component.form.controls.incidentDescription.setValue('Rear-ended');

  component.damagePhotos.set([
    new File(['x'], 'damage.jpg', { type: 'image/jpeg' })
  ]);

  component.submit();
  fixture.detectChanges();

  expect(api.submitClaim).not.toHaveBeenCalled();

  expect(fixture.nativeElement.textContent)
    .toContain('Police report is required.');
});

  it('disables submission and shows indeterminate handoff while the request is in progress', () => {
    const response = new Subject<unknown>();
    api.submitClaim.mockReturnValue(response);
    component.form.controls.incidentDescription.setValue('Rear-ended at a stoplight');
    component.damagePhotos.set([new File(['x'], 'damage.jpg', { type: 'image/jpeg' })]);

    component.submit();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Submitting your claim');
    expect(fixture.nativeElement.textContent).toContain('Uploading evidence and starting FirstNotice analysis');
    expect(fixture.nativeElement.querySelector('.indeterminate-progress')).not.toBeNull();
    expect((fixture.nativeElement.querySelector('button[type=submit]') as HTMLButtonElement).disabled).toBe(true);
    expect((fixture.nativeElement.querySelector('fieldset') as HTMLFieldSetElement).disabled).toBe(true);
    expect(fixture.nativeElement.textContent).not.toMatch(/\b\d+%\b/);
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it('ignores duplicate submit attempts while the original request is active', () => {
    api.submitClaim.mockReturnValue(new Subject<unknown>());
    component.form.controls.incidentDescription.setValue('Rear-ended');
    component.damagePhotos.set([new File(['x'], 'damage.jpg', { type: 'image/jpeg' })]);

    component.submit();
    component.submit();

    expect(api.submitClaim).toHaveBeenCalledOnce();
  });

  it('restores the form and existing error handling when submission fails', () => {
    api.submitClaim.mockReturnValue(throwError(() => new Error('network')));
    component.form.controls.incidentDescription.setValue('Rear-ended');
    component.damagePhotos.set([new File(['x'], 'damage.jpg', { type: 'image/jpeg' })]);

    component.submit();
    fixture.detectChanges();

    expect(component.submitting()).toBe(false);
    expect(fixture.nativeElement.querySelector('.submission-progress')).toBeNull();
    expect((fixture.nativeElement.querySelector('fieldset') as HTMLFieldSetElement).disabled).toBe(false);
    expect((fixture.nativeElement.querySelector('button[type=submit]') as HTMLButtonElement).disabled).toBe(false);
    expect(fixture.nativeElement.textContent).toContain('We could not submit your claim');
  });

  it('keeps the untested voice-note control out of the claimant form', () => {
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).not.toContain('Voice note');
    expect(fixture.nativeElement.querySelector('input[type=file][accept*="audio"]')).toBeNull();
  });

  it('reuses the same idempotency key when a submission is retried', () => {
    api.submitClaim.mockReturnValueOnce(throwError(() => new Error('network')))
      .mockReturnValueOnce(of(new HttpResponse({ body: {
        claim_id: 'CLM-ABC12345', status: 'new', event_id: 'evt', message: 'received',
      }})));
    component.form.controls.incidentDescription.setValue('Rear-ended');
    component.damagePhotos.set([new File(['x'], 'damage.jpg', { type: 'image/jpeg' })]);

    component.submit();
    component.submit();

    expect(api.submitClaim.mock.calls[0][1]).toBe(api.submitClaim.mock.calls[1][1]);
    expect(component.requestError()).toBe('');
  });
});
