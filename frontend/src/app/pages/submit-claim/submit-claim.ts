import { HttpEventType } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { FormBuilder, ReactiveFormsModule } from '@angular/forms';
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
    incidentDescription: [''],
  });
  readonly evidenceFiles = signal<File[]>([]);
  readonly errors = signal<Record<string, string>>({});
  readonly submitting = signal(false);
  readonly requestError = signal('');

  chooseEvidence(event: Event): void {
    const input = event.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    const unsupported = files.some((file) => !EVIDENCE_TYPES.has(file.type));
    if (unsupported) {
      this.setFileError('evidence', 'Use PDF, JPG, JPEG, or PNG files only.');
    } else if (files.length) {
      this.evidenceFiles.update((selected) => {
        const seen = new Set(selected.map((file) => this.fileKey(file)));
        return [
          ...selected,
          ...files.filter((file) => {
            const key = this.fileKey(file);
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
          }),
        ];
      });
      this.setFileError('evidence', '');
    }
    input.value = '';
  }

  removeEvidence(fileToRemove: File): void {
    this.evidenceFiles.update((files) =>
      files.filter((file) => this.fileKey(file) !== this.fileKey(fileToRemove)),
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

  private fileKey(file: File): string {
    return `${file.name}:${file.size}:${file.lastModified}:${file.type}`;
  }
}
