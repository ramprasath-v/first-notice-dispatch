import { HttpEventType } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router } from '@angular/router';
import { ClaimApiService } from '../../core/services/claim-api.service';

const EVIDENCE_TYPES = new Set(['application/pdf', 'image/jpeg', 'image/png']);

@Component({
  selector: 'app-submit-claim',
  imports: [ReactiveFormsModule],
  templateUrl: './submit-claim.html',
  styleUrl: './submit-claim.scss',
})
export class SubmitClaim {
  private readonly fb = inject(FormBuilder);
  private readonly api = inject(ClaimApiService);
  private readonly router = inject(Router);
  private readonly idempotencyKey = crypto.randomUUID();

  readonly form = this.fb.nonNullable.group({
    incidentDescription: ['', [Validators.maxLength(4000)]],
    policyNumberHint: ['', [Validators.required, Validators.maxLength(128)]],
  });
  readonly evidenceFiles = signal<File[]>([]);
  readonly errors = signal<Record<string, string>>({});
  readonly submitting = signal(false);
  readonly requestError = signal('');

  chooseEvidence(event: Event): void {
    const files = Array.from((event.target as HTMLInputElement).files ?? []);
    this.evidenceFiles.set(files);
    this.setFileError(
      'evidence',
      files.length && files.every((file) => EVIDENCE_TYPES.has(file.type))
        ? ''
        : files.length
          ? 'Use PDF, JPG, JPEG, or PNG files only.'
          : 'Add at least one evidence file.',
    );
  }

  submit(): void {
    if (this.submitting()) return;
    this.form.markAllAsTouched();
    this.chooseRequiredFileErrors();
    if (this.form.invalid || Object.values(this.errors()).some(Boolean)) return;
    this.submitting.set(true);
    this.requestError.set('');
    const value = this.form.getRawValue();
    this.api
      .submitClaim(
        {
          incidentDescription: value.incidentDescription,
          policyNumberHint: value.policyNumberHint,
          evidenceFiles: this.evidenceFiles(),
        },
        this.idempotencyKey,
      )
      .subscribe({
        next: (event) => {
          if (event.type === HttpEventType.Response && event.body) {
            void this.router.navigate(['/claims', event.body.claim_id]);
          }
        },
        error: () => {
          this.submitting.set(false);
          this.requestError.set(
            'We could not submit your claim. Your files were not lost—please try again.',
          );
        },
      });
  }

  private chooseRequiredFileErrors(): void {
    if (!this.evidenceFiles().length) {
      this.setFileError('evidence', 'Add at least one evidence file.');
    }
  }

  private setFileError(key: string, message: string): void {
    this.errors.update((errors) => ({ ...errors, [key]: message }));
  }
}
