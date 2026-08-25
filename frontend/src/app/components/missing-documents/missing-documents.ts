import { Component, EventEmitter, Input, Output, signal } from '@angular/core';
import { ClaimantActionDisplay, ClaimantEvidenceRequest } from '../../models/claim';

export interface DocumentUploadRequest {
  documentType: string;
  file: File;
  requestedActionId?: string;
  idempotencyKey?: string;
}

@Component({
  selector: 'app-missing-documents',
  templateUrl: './missing-documents.html',
  styleUrl: './missing-documents.scss',
})
export class MissingDocuments {
  @Input({ required: true }) requests: ClaimantEvidenceRequest[] = [];
  @Input() uploading = false;
  @Input() processing = false;
  @Input() displayReason: ClaimantActionDisplay | null = null;
  @Output() uploadDocuments = new EventEmitter<DocumentUploadRequest[]>();
  readonly selected = signal<Record<string, { file: File; idempotencyKey: string }>>({});
  readonly dragActiveKey = signal<string | null>(null);

  key(request: ClaimantEvidenceRequest): string {
    return request.requested_action_id || request.document_type;
  }

  choose(request: ClaimantEvidenceRequest, event: Event): void {
    const file = (event.target as HTMLInputElement).files?.[0];
    if (file) this.select(request, file);
  }

  dragOver(request: ClaimantEvidenceRequest, event: DragEvent): void {
    event.preventDefault();
    if (this.uploading || this.processing) return;
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    this.dragActiveKey.set(this.key(request));
  }

  dragLeave(event: DragEvent): void {
    event.preventDefault();
    this.dragActiveKey.set(null);
  }

  drop(request: ClaimantEvidenceRequest, event: DragEvent): void {
    event.preventDefault();
    this.dragActiveKey.set(null);
    if (this.uploading || this.processing) return;
    const file = event.dataTransfer?.files?.[0];
    if (file) this.select(request, file);
  }

  private select(request: ClaimantEvidenceRequest, file: File): void {
    this.selected.update((files) => ({
      ...files,
      [this.key(request)]: { file, idempotencyKey: crypto.randomUUID() },
    }));
  }

  remove(request: ClaimantEvidenceRequest): void {
    const key = this.key(request);
    this.selected.update((files) => {
      const next = { ...files };
      delete next[key];
      return next;
    });
  }

  uploadAll(): void {
    const uploads = this.requests.flatMap((request) => {
      const selected = this.selected()[this.key(request)];
      return selected ? [{
        documentType: request.document_type,
        file: selected.file,
        requestedActionId: request.requested_action_id,
        idempotencyKey: request.requested_action_id ? selected.idempotencyKey : undefined,
      }] : [];
    });
    if (uploads.length) this.uploadDocuments.emit(uploads);
  }

  allSelected(): boolean {
    return this.requests.length > 0 && this.requests.every(
      (request) => !!this.selected()[this.key(request)],
    );
  }
}
