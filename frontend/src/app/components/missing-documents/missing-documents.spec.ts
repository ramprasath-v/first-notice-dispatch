import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ClaimantEvidenceRequest } from '../../models/claim';
import { MissingDocuments } from './missing-documents';

describe('MissingDocuments', () => {
  let fixture: ComponentFixture<MissingDocuments>;
  let component: MissingDocuments;
  const request: ClaimantEvidenceRequest = {
    document_type: 'license_plate_photo',
    label: 'License Plate Photo',
    instruction: 'Please upload a clear vehicle photo with a readable license plate.',
    satisfies_requirements: ['vehicle_identity', 'license_plate_photo'],
    replacement_required: false,
    requested_action_id: 'ACT-FLOW-4',
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [MissingDocuments] }).compileComponents();
    fixture = TestBed.createComponent(MissingDocuments);
    component = fixture.componentInstance;
    component.requests = [request];
    fixture.detectChanges();
  });

  it('drops one corrective file through the existing requested-action upload path', () => {
    const emitted = vi.fn();
    component.uploadDocuments.subscribe(emitted);
    const first = new File(['first'], 'wrong.jpg', { type: 'image/jpeg' });
    const ignored = new File(['second'], 'ignored.jpg', { type: 'image/jpeg' });

    component.drop(request, {
      preventDefault: vi.fn(),
      dataTransfer: { files: [first, ignored] },
    } as unknown as DragEvent);
    component.uploadAll();

    const uploads = emitted.mock.calls[0][0];
    expect(uploads).toHaveLength(1);
    expect(uploads[0].file).toBe(first);
    expect(uploads[0].requestedActionId).toBe('ACT-FLOW-4');
    expect(uploads[0].idempotencyKey).toBeTruthy();
  });

  it('keeps Browse selection behavior and one-file replacement semantics', () => {
    const browseFile = new File(['browse'], 'browse.png', { type: 'image/png' });
    component.choose(request, {
      target: { files: [browseFile] },
    } as unknown as Event);

    expect(component.selected()[component.key(request)].file).toBe(browseFile);
  });

  it('sets and clears the remediation drag-over state', () => {
    const preventDefault = vi.fn();
    component.dragOver(request, {
      preventDefault,
      dataTransfer: { dropEffect: 'none' },
    } as unknown as DragEvent);
    fixture.detectChanges();

    expect(component.dragActiveKey()).toBe('ACT-FLOW-4');
    expect(fixture.nativeElement.querySelector('.file-button.drag-active')).not.toBeNull();

    component.dragLeave({ preventDefault } as unknown as DragEvent);
    expect(component.dragActiveKey()).toBeNull();
  });
});
