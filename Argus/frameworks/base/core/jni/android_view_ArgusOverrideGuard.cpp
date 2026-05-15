/*
 * Core ARGUS addition for
 * frameworks/base/core/jni/android_view_ArgusOverrideGuard.cpp.
 */

__attribute__((weak)) extern "C" jboolean ArtMethodCallsSuper(JNIEnv*, jobject, jstring, jstring);

static jboolean nativeMethodCallsSuper(JNIEnv* env, jclass, jobject reflected_method,
        jstring super_class_name, jstring method_name) {
    if (ArtMethodCallsSuper == nullptr || reflected_method == nullptr) {
        return JNI_TRUE;
    }
    return ArtMethodCallsSuper(env, reflected_method, super_class_name, method_name);
}

static const JNINativeMethod gArgusOverrideGuardMethods[] = {
    { "nativeMethodCallsSuper", "(Ljava/lang/reflect/Method;Ljava/lang/String;Ljava/lang/String;)Z",
            reinterpret_cast<void*>(nativeMethodCallsSuper) },
};
