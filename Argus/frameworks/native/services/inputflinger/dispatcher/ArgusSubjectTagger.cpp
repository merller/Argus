/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */

#define LOG_TAG "ArgusSubjectTagger"

#include "ArgusSubjectTagger.h"

#include "ArgusKernelInputGate.h"

#include <android-base/logging.h>
#include <openssl/mem.h>
#include <sys/random.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <limits>

namespace android::inputdispatcher {
namespace {

struct AuthenticatedMaterial {
    int32_t eventId;
    uint32_t reserved;
    uint64_t counter;
    int32_t targetPid;
    int32_t targetUid;
};

static_assert(sizeof(AuthenticatedMaterial) == 24);

uint64_t rotateLeft(uint64_t value, int bits) {
    return (value << bits) | (value >> (64 - bits));
}

void sipRound(uint64_t* v0, uint64_t* v1, uint64_t* v2, uint64_t* v3) {
    *v0 += *v1;
    *v1 = rotateLeft(*v1, 13);
    *v1 ^= *v0;
    *v0 = rotateLeft(*v0, 32);
    *v2 += *v3;
    *v3 = rotateLeft(*v3, 16);
    *v3 ^= *v2;
    *v0 += *v3;
    *v3 = rotateLeft(*v3, 21);
    *v3 ^= *v0;
    *v2 += *v1;
    *v1 = rotateLeft(*v1, 17);
    *v1 ^= *v2;
    *v2 = rotateLeft(*v2, 32);
}

uint64_t sipHash24(const std::array<uint64_t, 2>& key,
                   const uint8_t* bytes, size_t size) {
    uint64_t v0 = UINT64_C(0x736f6d6570736575) ^ key[0];
    uint64_t v1 = UINT64_C(0x646f72616e646f6d) ^ key[1];
    uint64_t v2 = UINT64_C(0x6c7967656e657261) ^ key[0];
    uint64_t v3 = UINT64_C(0x7465646279746573) ^ key[1];

    const uint8_t* end = bytes + (size & ~static_cast<size_t>(7));
    while (bytes != end) {
        uint64_t block;
        memcpy(&block, bytes, sizeof(block));
        v3 ^= block;
        sipRound(&v0, &v1, &v2, &v3);
        sipRound(&v0, &v1, &v2, &v3);
        v0 ^= block;
        bytes += sizeof(block);
    }

    uint64_t tail = static_cast<uint64_t>(size) << 56;
    for (size_t index = 0; index < (size & 7); ++index) {
        tail |= static_cast<uint64_t>(bytes[index]) << (index * 8);
    }
    v3 ^= tail;
    sipRound(&v0, &v1, &v2, &v3);
    sipRound(&v0, &v1, &v2, &v3);
    v0 ^= tail;
    v2 ^= 0xff;
    for (int round = 0; round < 4; ++round) {
        sipRound(&v0, &v1, &v2, &v3);
    }
    return v0 ^ v1 ^ v2 ^ v3;
}

void randomKey(std::array<uint64_t, 2>* key) {
    uint8_t* output = reinterpret_cast<uint8_t*>(key->data());
    size_t remaining = sizeof(*key);
    while (remaining != 0) {
        const ssize_t read = getrandom(output, remaining, 0);
        if (read > 0) {
            output += read;
            remaining -= static_cast<size_t>(read);
            continue;
        }
        if (read < 0 && errno == EINTR) {
            continue;
        }
        LOG(FATAL) << "getrandom failed while creating the Argus InputDispatcher key: "
                   << strerror(errno);
    }
}

bool isAgentSource(ArgusSubjectSource source) {
    return source == ArgusSubjectSource::AGENT;
}

}  // namespace

ArgusSubjectTagger::ArgusSubjectTagger() {
    randomKey(&mAgentKey);
    randomKey(&mUserKey);
    CHECK(mAgentKey != mUserKey);
}

ArgusSubjectTagger::~ArgusSubjectTagger() {
    OPENSSL_cleanse(mAgentKey.data(), sizeof(mAgentKey));
    OPENSSL_cleanse(mUserKey.data(), sizeof(mUserKey));
}

ArgusSubjectTag ArgusSubjectTagger::mint(ArgusSubjectSource source, int32_t eventId,
                                         int32_t targetPid, int32_t targetUid) {
    CHECK(source == ArgusSubjectSource::PHYSICAL_USER || isAgentSource(source));
    CHECK_GE(eventId, 0);
    CHECK_GT(targetPid, 0);
    CHECK_GE(targetUid, 0);

    ArgusSubjectTag tag{
            .eventId = eventId,
            .counter = mNextCounter.fetch_add(1, std::memory_order_relaxed),
    };
    CHECK_NE(tag.counter, 0u) << "Argus subject counter exhausted";
    const auto& key = isAgentSource(source) ? mAgentKey : mUserKey;
    tag.authenticator = authenticate(tag, targetPid, targetUid, key);
    CHECK_NE(tag.authenticator, 0u);
    return tag;
}

ArgusSubjectSource ArgusSubjectTagger::verify(const ArgusSubjectTag& tag,
                                              int32_t callingPid, int32_t callingUid) {
    if (!tag.isPresent()) {
        return ArgusSubjectSource::TAMPERED;
    }

    const uint64_t next = mNextCounter.load(std::memory_order_acquire);
    if (tag.counter >= next || next - tag.counter > kReplayWindow) {
        return ArgusSubjectSource::TAMPERED;
    }
    const uint64_t expectedAgent = authenticate(
            tag, callingPid, callingUid, mAgentKey);
    const uint64_t expectedUser = authenticate(
            tag, callingPid, callingUid, mUserKey);
    const bool isAgent =
            CRYPTO_memcmp(&expectedAgent, &tag.authenticator, sizeof(expectedAgent)) == 0;
    const bool isUser =
            CRYPTO_memcmp(&expectedUser, &tag.authenticator, sizeof(expectedUser)) == 0;
    if (isAgent == isUser) {
        return ArgusSubjectSource::TAMPERED;
    }

    std::lock_guard<std::mutex> lock(mReplayLock);
    const ConsumedTag consumed{.uid = callingUid, .counter = tag.counter};
    if (!mConsumed.insert(consumed).second) {
        return ArgusSubjectSource::TAMPERED;
    }
    pruneConsumedLocked(next > kReplayWindow ? next - kReplayWindow : 0);
    return isAgent ? ArgusSubjectSource::AGENT : ArgusSubjectSource::PHYSICAL_USER;
}

uint64_t ArgusSubjectTagger::authenticate(const ArgusSubjectTag& tag,
                                          int32_t pid, int32_t uid,
                                          const std::array<uint64_t, 2>& key) const {
    const AuthenticatedMaterial material{
            .eventId = tag.eventId,
            .reserved = tag.reserved,
            .counter = tag.counter,
            .targetPid = pid,
            .targetUid = uid,
    };
    return sipHash24(key, reinterpret_cast<const uint8_t*>(&material), sizeof(material));
}

void ArgusSubjectTagger::pruneConsumedLocked(uint64_t minimumCounter) {
    for (auto iterator = mConsumed.begin(); iterator != mConsumed.end();) {
        if (iterator->counter < minimumCounter) {
            iterator = mConsumed.erase(iterator);
        } else {
            ++iterator;
        }
    }
}

ArgusSubjectTagger& GetArgusSubjectTagger() {
    /* Constructed by InputDispatcher startup and retained only in system_server. */
    static ArgusSubjectTagger tagger;
    return tagger;
}

extern "C" int32_t ArgusVerifySubjectTag(const ArgusSubjectTag* tag,
                                         int32_t callingPid, int32_t callingUid) {
    if (tag == nullptr || callingPid <= 0 || callingUid < 0) {
        return static_cast<int32_t>(ArgusSubjectSource::TAMPERED);
    }
    return static_cast<int32_t>(
            GetArgusSubjectTagger().verify(*tag, callingPid, callingUid));
}

extern "C" bool ArgusMintAccessibilityTag(int32_t eventId,
                                          int32_t targetPid, int32_t targetUid,
                                          ArgusSubjectTag* outTag) {
    if (outTag == nullptr || eventId < 0 || targetPid <= 0 || targetUid < 0) {
        return false;
    }
    if (!ArgusKernelAllowsAgentInput(ArgusSubjectSource::ACCESSIBILITY)) {
        return false;
    }
    *outTag = GetArgusSubjectTagger().mint(ArgusSubjectSource::ACCESSIBILITY,
                                           eventId, targetPid, targetUid);
    return true;
}

}  // namespace android::inputdispatcher
