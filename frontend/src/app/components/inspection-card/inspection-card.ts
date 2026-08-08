import { DatePipe, TitleCasePipe } from '@angular/common';
import { Component, Input } from '@angular/core';
import { InspectionAppointment } from '../../models/claim';

@Component({
  selector: 'app-inspection-card',
  imports: [DatePipe, TitleCasePipe],
  templateUrl: './inspection-card.html',
  styleUrl: './inspection-card.scss',
})
export class InspectionCard {
  @Input({ required: true }) inspection!: InspectionAppointment;
}
