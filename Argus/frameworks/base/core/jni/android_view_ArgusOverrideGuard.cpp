/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */

#include <jni.h>
#include <nativehelper/JNIHelp.h>

#include "core_jni_helpers.h"

extern "C" __attribute__((weak)) jboolean ArtMethodCallsSuper(
        JNIEnv*, jobject, jstring, jstring);

namespace android {
namespace {

static jboolean nativeMethodCallsSuper(JNIEnv* env, jclass, jobject reflected_method,
        jstring super_class_name, jstring method_name) {
    if (ArtMethodCallsSuper == nullptr || reflected_method == nullptr) {
        return JNI_TRUE;
    }
    return ArtMethodCallsSuper(env, reflected_method, super_class_name, method_name);
}

const JNINativeMethod gArgusOverrideGuardMethods[] = {
    { "nativeMethodCallsSuper", "(Ljava/lang/reflect/Method;Ljava/lang/String;Ljava/lang/String;)Z",
            reinterpret_cast<void*>(nativeMethodCallsSuper) },
};

}  // namespace

int register_android_view_ArgusOverrideGuard(JNIEnv* env) {
    RegisterMethodsOrDie(env, "android/view/ArgusOverrideGuard",
                         gArgusOverrideGuardMethods, NELEM(gArgusOverrideGuardMethods));
    return 0;
}

}  // namespace android
