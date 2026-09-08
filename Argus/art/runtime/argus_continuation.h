#ifndef ART_RUNTIME_ARGUS_CONTINUATION_H_
#define ART_RUNTIME_ARGUS_CONTINUATION_H_

#include <cstdint>

#include "base/locks.h"

namespace art {

class ArtMethod;
class ShadowFrame;

// Called only from the switch interpreter.  The implementation is a no-op unless a
// framework dispatch point has armed an injected Argus operation on this thread.
bool ArgusContinuationObserveCall(ArtMethod* caller,
                                  uint32_t dex_pc,
                                  ArtMethod* actual_target,
                                  int invoke_type)
    REQUIRES_SHARED(Locks::mutator_lock_);

bool ArgusContinuationObserveBranch(ArtMethod* method,
                                    uint32_t dex_pc,
                                    int32_t selected_offset,
                                    bool selector_candidate)
    REQUIRES_SHARED(Locks::mutator_lock_);

// Marks entry into the exact callback ArtMethod selected by the framework.  This is
// deliberately checked from the callback's own switch-interpreter frame: the
// framework-to-callback invoke can originate in boot-image compiled code and is not
// therefore a reliable observation point.
void ArgusContinuationMethodPreamble(ShadowFrame* frame, ArtMethod* method)
    REQUIRES_SHARED(Locks::mutator_lock_);

// Seals the scope from ART itself when the exact callback frame leaves Execute(),
// whether by return, exception unwind, or Argus force-pop.  The framework-side End
// call may collect a sealed result but can never cancel an executing scope.
void ArgusContinuationMethodExit(ShadowFrame* frame)
    REQUIRES_SHARED(Locks::mutator_lock_);

// A denied edge requests a non-standard return.  This predicate propagates that
// request through interpreted descendants until the certified callback frame is
// reached, without throwing an application-catchable exception.
bool ArgusContinuationShouldForceReturn(ShadowFrame* frame)
    REQUIRES_SHARED(Locks::mutator_lock_);

}  // namespace art

#endif  // ART_RUNTIME_ARGUS_CONTINUATION_H_
