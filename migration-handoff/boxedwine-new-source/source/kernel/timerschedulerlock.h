#ifndef BOXEDWINE_TIMER_SCHEDULER_LOCK_H
#define BOXEDWINE_TIMER_SCHEDULER_LOCK_H

#ifdef BOXEDWINE_MULTI_THREADED
#include <mutex>
// Timer callbacks take object conditions under the scheduler lock. Mutations
// involving those conditions must acquire the same outer lock first.
std::unique_lock<std::recursive_mutex> lockTimerScheduler();
#endif

#endif
