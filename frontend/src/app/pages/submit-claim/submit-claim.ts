import { HttpEventType } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router } from '@angular/router';
import { ClaimApiService } from '../../core/services/claim-api.service';

const PHOTO_TYPES = new Set(['image/jpeg', 'image/png']);
const AUDIO_TYPES = new Set(['audio/mpeg', 'audio/wav', 'audio/x-wav', 'audio/mp4']);

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
    incidentDescription: ['', [Validators.required, Validators.maxLength(4000)]],
    policyNumberHint: ['', Validators.maxLength(128)],
  });
  readonly damagePhotos = signal<File[]>([]);
  readonly policeReport = signal<File | null>(null);
  readonly audio = signal<File | null>(null);
  readonly errors = signal<Record<string, string>>({});
  readonly submitting = signal(false);
  readonly requestError = signal('');

  choosePhotos(event: Event): void {
    const files = Array.from((event.target as HTMLInputElement).files ?? []);
    this.damagePhotos.set(files);
    this.setFileError('photos', files.length && files.every((f) => PHOTO_TYPES.has(f.type))
      ? '' : 'Choose at least one JPEG or PNG damage photo.');
  }

  choosePoliceReport(event: Event): void {
    const file = (event.target as HTMLInputElement).files?.[0] ?? null;
    this.policeReport.set(file);
    this.setFileError('police', !file || file.type === 'application/pdf' ? '' : 'Choose a PDF police report.');
  }

  chooseAudio(event: Event): void {
    const file = (event.target as HTMLInputElement).files?.[0] ?? null;
    this.audio.set(file);
    this.setFileError('audio', !file || AUDIO_TYPES.has(file.type) ? '' : 'Choose an MP3, WAV, or MP4 audio file.');
  }

  submit(): void {
    if (this.submitting()) return;
    this.form.markAllAsTouched();
    this.chooseRequiredFileErrors();
    if (this.form.invalid || Object.values(this.errors()).some(Boolean)) return;
    this.submitting.set(true);
    this.requestError.set('');
    const value = this.form.getRawValue();
    this.api.submitClaim({
      incidentDescription: value.incidentDescription,
      policyNumberHint: value.policyNumberHint || undefined,
      damagePhotos: this.damagePhotos(),
      policeReport: this.policeReport() ?? undefined,
      audio: this.audio() ?? undefined,
    }, this.idempotencyKey).subscribe({
      next: (event) => {
        if (event.type === HttpEventType.Response && event.body) {
          void this.router.navigate(['/claims', event.body.claim_id]);
        }
      },
      error: () => {
        this.submitting.set(false);
        this.requestError.set('We could not submit your claim. Your files were not lost—please try again.');
      },
    });
  }

  private chooseRequiredFileErrors(): void {
    if (!this.damagePhotos().length) this.setFileError('photos', 'Add at least one damage photo.');
  }

  private setFileError(key: string, message: string): void {
    this.errors.update((errors) => ({ ...errors, [key]: message }));
  }
}
