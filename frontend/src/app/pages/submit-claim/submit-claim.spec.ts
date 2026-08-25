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

  function setValidSubmission(): void {
    component.evidenceFiles.set([
      new File(['report'], 'police-report.png', { type: 'image/png' }),
      new File(['photo'], 'vehicle.jpg', { type: 'image/jpeg' }),
    ]);
  }

  it('does not show or require a policy number', () => {
    fixture.detectChanges();
    expect(component.form.contains('policyNumberHint')).toBe(false);
    expect(fixture.nativeElement.textContent).not.toContain('Policy number');
    expect(fixture.nativeElement.querySelector('#policy')).toBeNull();
  });

  it('explains the evidence-first intake model without an incident-date field', () => {
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Upload evidence, don’t fill out forms.');
    expect(fixture.nativeElement.textContent).toContain(
      'FirstNotice extracts claim details from your documents and photos automatically.',
    );
    expect(fixture.nativeElement.textContent).not.toContain(
      'Upload your claim evidence. FirstNotice will sort it automatically.',
    );
    expect(fixture.nativeElement.textContent).not.toContain(
      'We only ask you for information when it can’t be determined reliably.',
    );
    expect(fixture.nativeElement.querySelector('input[type=date]')).toBeNull();
  });

  it('rejects submission without evidence', () => {
    component.submit();
    fixture.detectChanges();
    expect(api.submitClaim).not.toHaveBeenCalled();
    expect(fixture.nativeElement.textContent).toContain('Add at least one evidence file');
  });

  it('rejects unsupported evidence types', () => {
    component.chooseEvidence({
      target: { files: [new File(['x'], 'notes.txt', { type: 'text/plain' })] },
    } as unknown as Event);
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Use PDF, JPG, JPEG, or PNG files only');
  });

  it('uses one multi-file control and lists selected filenames', () => {
    setValidSubmission();
    fixture.detectChanges();

    const inputs = fixture.nativeElement.querySelectorAll('input[type=file]');
    expect(inputs).toHaveLength(1);
    expect((inputs[0] as HTMLInputElement).multiple).toBe(true);
    expect((inputs[0] as HTMLInputElement).accept).toContain('.pdf');
    expect(fixture.nativeElement.textContent).toContain('police-report.png');
    expect(fixture.nativeElement.textContent).toContain('vehicle.jpg');
    expect(fixture.nativeElement.textContent).not.toContain('Damage photos');
    expect(fixture.nativeElement.textContent).not.toContain('Required · PDF');
  });

  it('appends selections and removes only the chosen file', () => {
    const a = new File(['a'], 'a.jpg', { type: 'image/jpeg', lastModified: 1 });
    const b = new File(['b'], 'b.pdf', { type: 'application/pdf', lastModified: 2 });
    const c = new File(['c'], 'c.png', { type: 'image/png', lastModified: 3 });

    component.chooseEvidence({ target: { files: [a, b], value: 'selected' } } as unknown as Event);
    component.removeEvidence(b);
    component.chooseEvidence({ target: { files: [c], value: 'selected' } } as unknown as Event);

    expect(component.evidenceFiles().map((file) => file.name)).toEqual(['a.jpg', 'c.png']);
  });

  it('preserves existing files and ignores deterministic duplicates', () => {
    const a = new File(['a'], 'a.jpg', { type: 'image/jpeg', lastModified: 1 });
    const b = new File(['b'], 'b.pdf', { type: 'application/pdf', lastModified: 2 });

    component.chooseEvidence({ target: { files: [a], value: 'selected' } } as unknown as Event);
    component.chooseEvidence({ target: { files: [a, b], value: 'selected' } } as unknown as Event);

    expect(component.evidenceFiles().map((file) => file.name)).toEqual(['a.jpg', 'b.pdf']);
  });

  it('submits exactly the currently selected files', () => {
    api.submitClaim.mockReturnValue(new Subject<unknown>());
    const kept = new File(['a'], 'kept.jpg', { type: 'image/jpeg', lastModified: 1 });
    const removed = new File(['b'], 'removed.pdf', { type: 'application/pdf', lastModified: 2 });
    component.evidenceFiles.set([kept, removed]);
    component.removeEvidence(removed);

    component.submit();

    expect(api.submitClaim.mock.calls[0][0].evidenceFiles).toEqual([kept]);
    expect(api.submitClaim.mock.calls[0][0].policyNumberHint).toBeUndefined();
  });

  it('disables submission and shows indeterminate handoff while the request is in progress', () => {
    const response = new Subject<unknown>();
    api.submitClaim.mockReturnValue(response);
    setValidSubmission();

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
    setValidSubmission();

    component.submit();
    component.submit();

    expect(api.submitClaim).toHaveBeenCalledOnce();
  });

  it('restores the form and existing error handling when submission fails', () => {
    api.submitClaim.mockReturnValue(throwError(() => new Error('network')));
    setValidSubmission();

    component.submit();
    fixture.detectChanges();

    expect(component.submitting()).toBe(false);
    expect(fixture.nativeElement.querySelector('.submission-progress')).toBeNull();
    expect((fixture.nativeElement.querySelector('fieldset') as HTMLFieldSetElement).disabled).toBe(false);
    expect((fixture.nativeElement.querySelector('button[type=submit]') as HTMLButtonElement).disabled).toBe(false);
    expect(fixture.nativeElement.textContent).toContain('We could not submit your claim');
  });

  it('reuses the same idempotency key when a submission is retried', () => {
    api.submitClaim.mockReturnValueOnce(throwError(() => new Error('network')))
      .mockReturnValueOnce(of(new HttpResponse({ body: {
        claim_id: 'CLM-ABC12345', status: 'new', event_id: 'evt', message: 'received',
      }})));
    setValidSubmission();

    component.submit();
    component.submit();

    expect(api.submitClaim.mock.calls[0][1]).toBe(api.submitClaim.mock.calls[1][1]);
    expect(component.requestError()).toBe('');
  });
});
