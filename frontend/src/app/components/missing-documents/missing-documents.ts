import { Component, EventEmitter, Input, Output, signal } from '@angular/core';
import { ClaimantEvidenceRequest } from '../../models/claim';

export interface DocumentUploadRequest { documentType: string; file: File }

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
  readonly selected = signal<Record<string, File>>({});

  choose(request: ClaimantEvidenceRequest, event: Event): void {
    const file = (event.target as HTMLInputElement).files?.[0];
    if (file) this.selected.update((files) => ({ ...files, [request.document_type]: file }));
  }

  upload(request: ClaimantEvidenceRequest): void {
    const documentType = request.document_type;
    const file = this.selected()[documentType];
    if (file) this.uploadDocument.emit({ documentType, file });
  }
}
