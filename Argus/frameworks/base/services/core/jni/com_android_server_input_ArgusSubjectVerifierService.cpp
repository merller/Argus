/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */

#include <input/ArgusSubjectTag.h>
#include <nativehelper/JNIHelp.h>

#include "core_jni_helpers.h"

using android::ArgusSubjectSource;
using android::ArgusSubjectTag;

extern "C" int32_t ArgusVerifySubjectTag(const ArgusSubjectTag*, int32_t, int32_t);
extern "C" bool ArgusMintAccessibilityTag(int32_t, int32_t, int32_t, ArgusSubjectTag*);

namespace android {
namespace {

jint nativeVerifySubjectTag(JNIEnv*, jclass, jint eventId, jlong counter,
                            jlong authenticator, jint callingPid, jint callingUid) {
    const ArgusSubjectTag tag{
            .eventId = eventId,
            .counter = static_cast<uint64_t>(counter),
            .authenticator = static_cast<uint64_t>(authenticator),
    };
    return ArgusVerifySubjectTag(&tag, callingPid, callingUid);
}

jlongArray nativeMintAccessibilityTag(JNIEnv* env, jclass, jint eventId,
                                      jint targetPid, jint targetUid) {
    ArgusSubjectTag tag;
    if (!ArgusMintAccessibilityTag(eventId, targetPid, targetUid, &tag)) {
        return nullptr;
    }
    const jlong values[] = {
            static_cast<jlong>(tag.eventId), static_cast<jlong>(tag.counter),
            static_cast<jlong>(tag.authenticator),
    };
    jlongArray result = env->NewLongArray(3);
    if (result != nullptr) env->SetLongArrayRegion(result, 0, 3, values);
    return result;
}

const JNINativeMethod gMethods[] = {
        {"nativeVerifySubjectTag", "(IJJII)I", reinterpret_cast<void*>(nativeVerifySubjectTag)},
        {"nativeMintAccessibilityTag", "(III)[J",
         reinterpret_cast<void*>(nativeMintAccessibilityTag)},
};

}  // namespace

int register_com_android_server_input_ArgusSubjectVerifierService(JNIEnv* env) {
    RegisterMethodsOrDie(env, "com/android/server/input/ArgusSubjectVerifierService",
                         gMethods, NELEM(gMethods));
    return 0;
}

}  // namespace android
