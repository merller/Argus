/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */

#include <jni.h>
#include <nativehelper/JNIHelp.h>

#include <cstdint>
#include <string>

#include "core_jni_helpers.h"

namespace android {

namespace {

static constexpr int kVerdictAllow = 0;
static constexpr int kVerdictBlock = 1;
static constexpr int kVerdictRestricted = 2;

extern "C" __attribute__((weak)) int ArtArgusLookupKernelPolicy(
        const char*, const char*, uint32_t, uint64_t*);

static std::string JStringToStdString(JNIEnv* env, jstring value) {
    if (value == nullptr) {
        return std::string();
    }
    const char* chars = env->GetStringUTFChars(value, nullptr);
    if (chars == nullptr) {
        return std::string();
    }
    std::string out(chars);
    env->ReleaseStringUTFChars(value, chars);
    return out;
}

static jlongArray nativeGetDecision(JNIEnv* env, jclass, jstring dciplus, jstring operation,
        jint agent_source) {
    const std::string digest = JStringToStdString(env, dciplus);
    const std::string action = JStringToStdString(env, operation);
    if (digest.size() != 64 || action.empty() || ArtArgusLookupKernelPolicy == nullptr) {
        const jlong fallback[] = {kVerdictRestricted, 0};
        jlongArray result = env->NewLongArray(2);
        if (result != nullptr) env->SetLongArrayRegion(result, 0, 2, fallback);
        return result;
    }
    uint64_t restrictedToken = 0;
    const int verdict = ArtArgusLookupKernelPolicy(
            digest.c_str(), action.c_str(), static_cast<uint32_t>(agent_source),
            &restrictedToken);
    const int checkedVerdict =
            verdict == kVerdictAllow || verdict == kVerdictBlock || verdict == kVerdictRestricted
            ? verdict : kVerdictRestricted;
    const jlong values[] = {checkedVerdict, static_cast<jlong>(restrictedToken)};
    jlongArray result = env->NewLongArray(2);
    if (result != nullptr) env->SetLongArrayRegion(result, 0, 2, values);
    return result;
}

const JNINativeMethod gArgusRestrictedPolicyMethods[] = {
    { "nativeGetDecision", "(Ljava/lang/String;Ljava/lang/String;I)[J",
            reinterpret_cast<void*>(nativeGetDecision) },
};

}  // namespace

int register_android_view_ArgusRestrictedPolicy(JNIEnv* env) {
    RegisterMethodsOrDie(env, "android/view/ArgusRestrictedPolicy",
                         gArgusRestrictedPolicyMethods,
                         NELEM(gArgusRestrictedPolicyMethods));
    return 0;
}

}  // namespace android
