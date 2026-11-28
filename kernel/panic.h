#ifndef NOVUS_PANIC_H
#define NOVUS_PANIC_H

/* Stop the machine on purpose: log the stop code, paint the blue screen,
 * park every core. Never returns. `code` is the stop code (ASCII, no spaces
 * preferred), `detail` is free text and may contain newlines. */
void kpanic(const char *code, const char *detail);

/* True once any core has panicked; the timer path parks instead of
 * scheduling. */
int kpanic_raised(void);

#endif /* NOVUS_PANIC_H */
