import { Component, EventEmitter, Input, Output, signal } from '@angular/core';
import { ClaimantEvidenceRequest } from '../../models/claim';

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
  @Output() uploadDocument = new EventEmitter<DocumentUploadRequest>();
  readonly selected = signal<Record<string, { file: File; idempotencyKey: string }>>({});

  key(request: ClaimantEvidenceRequest): string {
    return request.requested_action_id || request.document_type;
  }

  choose(request: ClaimantEvidenceRequest, event: Event): void {
    const file = (event.target as HTMLInputElement).files?.[0];
    if (file) this.selected.update((files) => ({
      ...files,
      [this.key(request)]: { file, idempotencyKey: crypto.randomUUID() },
    }));
  }

  upload(request: ClaimantEvidenceRequest): void {
    const documentType = request.document_type;
    const selected = this.selected()[this.key(request)];
    if (selected) this.uploadDocument.emit({
      documentType,
      file: selected.file,
      requestedActionId: request.requested_action_id,
      idempotencyKey: request.requested_action_id ? selected.idempotencyKey : undefined,
    });
  }
}
