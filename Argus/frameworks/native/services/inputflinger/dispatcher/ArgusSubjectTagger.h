/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <input/ArgusSubjectTag.h>

#include <array>
#include <atomic>
#include <cstdint>
#include <mutex>
#include <unordered_set>

namespace android::inputdispatcher {

/* InputDispatcher-private issuer and one-shot verifier for subject tags. */
class ArgusSubjectTagger final {
public:
    static constexpr uint64_t kReplayWindow = 4096;

    ArgusSubjectTagger();
    ~ArgusSubjectTagger();

    ArgusSubjectTag mint(ArgusSubjectSource source, int32_t eventId,
                         int32_t targetPid, int32_t targetUid);

    /* Returns the authenticated source or TAMPERED.  Success consumes the tag. */
    ArgusSubjectSource verify(const ArgusSubjectTag& tag,
                              int32_t callingPid, int32_t callingUid);

    ArgusSubjectTagger(const ArgusSubjectTagger&) = delete;
    ArgusSubjectTagger& operator=(const ArgusSubjectTagger&) = delete;

private:
    struct ConsumedTag {
        int32_t uid;
        uint64_t counter;

        bool operator==(const ConsumedTag& other) const {
            return uid == other.uid && counter == other.counter;
        }
    };

    struct ConsumedTagHash {
        size_t operator()(const ConsumedTag& value) const {
            return (static_cast<size_t>(static_cast<uint32_t>(value.uid)) << 1) ^
                    static_cast<size_t>(value.counter) ^
                    static_cast<size_t>(value.counter >> 32);
        }
    };

    uint64_t authenticate(const ArgusSubjectTag& tag, int32_t pid, int32_t uid,
                          const std::array<uint64_t, 2>& key) const;
    void pruneConsumedLocked(uint64_t minimumCounter);

    std::array<uint64_t, 2> mAgentKey{};
    std::array<uint64_t, 2> mUserKey{};
    std::atomic<uint64_t> mNextCounter{1};
    std::mutex mReplayLock;
    std::unordered_set<ConsumedTag, ConsumedTagHash> mConsumed;
};

/*
 * These C entry points are used by the system_server Binder bridge.  They stay
 * in the inputflinger process image, so neither key ever enters an app process.
 */
extern "C" int32_t ArgusVerifySubjectTag(const ArgusSubjectTag* tag,
                                         int32_t callingPid, int32_t callingUid);
extern "C" bool ArgusMintAccessibilityTag(int32_t eventId,
                                          int32_t targetPid, int32_t targetUid,
                                          ArgusSubjectTag* outTag);

ArgusSubjectTagger& GetArgusSubjectTagger();

}  // namespace android::inputdispatcher
