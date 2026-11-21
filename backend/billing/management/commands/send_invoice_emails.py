"""Send pending invoice emails and retry failed ones.

The billing run makes one delivery attempt straight after it commits; this
command picks up everything that attempt didn't finish.

In production this would run on a schedule (e.g. cron).
"""
from django.core.management.base import BaseCommand

from billing.delivery import MAX_ATTEMPTS, retry_deliveries


class Command(BaseCommand):
    help = (
        'Send pending invoice emails and retry failed ones (up to '
        f'{MAX_ATTEMPTS} attempts). Meant to run on a schedule in production; '
        'not scheduled in this project.'
    )

    def handle(self, *args: str, **options: str | int | bool | None) -> None:
        summary = retry_deliveries()

        message = f'Sent {summary.sent}, failed {summary.failed}, skipped {summary.skipped}.'
        if summary.exhausted:
            message += (
                f' {summary.exhausted} deliveries have exhausted {MAX_ATTEMPTS} attempts'
                f' and need manual follow-up.'
            )
        style = self.style.WARNING if summary.failed or summary.exhausted else self.style.SUCCESS
        self.stdout.write(style(message))

        if summary.failed_ids:
            ids = ', '.join(str(pk) for pk in summary.failed_ids)
            self.stdout.write(self.style.WARNING(f'Failed delivery ids: {ids}'))
