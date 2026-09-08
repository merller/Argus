/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <cstdint>

namespace android {

/*
 * Authenticated provenance carried with one concrete input dispatch.
 *
 * Only InputDispatcher may mint or verify this value.  Application processes
 * may carry the opaque tuple in UI-thread TLS, but it contains no plaintext
 * provenance classification.  The verifier derives that classification by
 * authenticating with its independent user and agent keys.
 */
enum class ArgusSubjectSource : int32_t {
    UNKNOWN = 0,
    PHYSICAL_USER = 1,
    AGENT = 2,
    // Input modality is deliberately not part of the MAC subject.
    INJECTED = AGENT,
    ACCESSIBILITY = AGENT,
    IME = 4,
    TAMPERED = -1,
};

struct ArgusSubjectTag {
    int32_t eventId = -1;
    uint32_t reserved = 0;
    uint64_t counter = 0;
    uint64_t authenticator = 0;

    bool isPresent() const {
        return eventId >= 0 && reserved == 0 && counter != 0 && authenticator != 0;
    }
};

static_assert(sizeof(ArgusSubjectTag) == 24,
              "ArgusSubjectTag must have an identical 32/64-bit wire layout");

}  // namespace android
