/*
 * Core ARGUS additions for
 * frameworks/base/core/jni/android_view_AccessibilityClickMarker.cpp.
 */

// ART exports used by framework JNI. Weak symbols keep framework startup from
// failing if the ART-side research hooks are absent.
__attribute__((weak)) extern "C" jstring ArtGetDciCodebaseFromListener(JNIEnv*, jobject);
__attribute__((weak)) extern "C" jint    ArtGetDciIndexFromListener(JNIEnv*, jobject);
__attribute__((weak)) extern "C" jstring ArtGetDciCodebaseFromReflectedMethod(JNIEnv*, jobject);
__attribute__((weak)) extern "C" jint    ArtGetDciIndexFromReflectedMethod(JNIEnv*, jobject);

namespace {

static constexpr jint kSourceUser = 0;
static constexpr jint kSourceAccessibilityAction = 1;
static constexpr jint kSourceInjectedEvent = 2;
static constexpr jint kSourceAccessibilityGesture = 3;
static constexpr jint kTagTampered = -1;

static constexpr uint64_t kKeyAgent0 = 0xa9958f5d3a6b67d1ULL;
static constexpr uint64_t kKeyAgent1 = 0x6f0c2b19d9424a77ULL;
static constexpr uint64_t kKeyUser0 = 0xd11a2717ab42c53bULL;
static constexpr uint64_t kKeyUser1 = 0x84d3a291502b6d0fULL;

static thread_local uint64_t sWriteCounter = 1;
static thread_local uint64_t sVerifyCounter = 1;

struct ArgusTlsTag {
    uint64_t value = 0;
    uint64_t counter = 0;
};

static thread_local ArgusTlsTag sTlsTag;

static uint64_t Rotl(uint64_t value, int bits) {
    return (value << bits) | (value >> (64 - bits));
}

static uint64_t SipHash24(uint64_t key0, uint64_t key1, uint64_t message) {
    uint64_t v0 = 0x736f6d6570736575ULL ^ key0;
    uint64_t v1 = 0x646f72616e646f6dULL ^ key1;
    uint64_t v2 = 0x6c7967656e657261ULL ^ key0;
    uint64_t v3 = 0x7465646279746573ULL ^ key1;
    const uint64_t block = message;
    v3 ^= block;
    for (int i = 0; i < 2; ++i) {
        v0 += v1; v1 = Rotl(v1, 13); v1 ^= v0; v0 = Rotl(v0, 32);
        v2 += v3; v3 = Rotl(v3, 16); v3 ^= v2;
        v0 += v3; v3 = Rotl(v3, 21); v3 ^= v0;
        v2 += v1; v1 = Rotl(v1, 17); v1 ^= v2; v2 = Rotl(v2, 32);
    }
    v0 ^= block;
    v2 ^= 0xff;
    for (int i = 0; i < 4; ++i) {
        v0 += v1; v1 = Rotl(v1, 13); v1 ^= v0; v0 = Rotl(v0, 32);
        v2 += v3; v3 = Rotl(v3, 16); v3 ^= v2;
        v0 += v3; v3 = Rotl(v3, 21); v3 ^= v0;
        v2 += v1; v1 = Rotl(v1, 17); v1 ^= v2; v2 = Rotl(v2, 32);
    }
    return v0 ^ v1 ^ v2 ^ v3;
}

static bool IsAgentSource(jint source) {
    return source == kSourceAccessibilityAction
            || source == kSourceInjectedEvent
            || source == kSourceAccessibilityGesture;
}

static uint64_t MakeTag(jint source, uint64_t counter) {
    const uint64_t message = (static_cast<uint64_t>(static_cast<uint32_t>(source)) << 32)
            ^ counter;
    return IsAgentSource(source)
            ? SipHash24(kKeyAgent0, kKeyAgent1, message)
            : SipHash24(kKeyUser0, kKeyUser1, message);
}

static void nativeWriteSubjectTag(JNIEnv*, jclass, jint source) {
    const uint64_t counter = sWriteCounter++;
    sTlsTag.value = MakeTag(source, counter);
    sTlsTag.counter = counter;
}

static jint nativeVerifySubjectTag(JNIEnv*, jclass) {
    if (sTlsTag.value == 0 || sTlsTag.counter == 0) {
        sTlsTag = {};
        return kTagTampered;
    }
    const uint64_t counter = sVerifyCounter;
    if (counter != sTlsTag.counter) {
        sTlsTag = {};
        return kTagTampered;
    }
    const uint64_t userTag = MakeTag(kSourceUser, counter);
    if (sTlsTag.value == userTag) {
        ++sVerifyCounter;
        return kSourceUser;
    }
    const jint agentSources[] = {
        kSourceAccessibilityAction,
        kSourceInjectedEvent,
        kSourceAccessibilityGesture,
    };
    for (jint source : agentSources) {
        if (sTlsTag.value == MakeTag(source, counter)) {
            ++sVerifyCounter;
            return source;
        }
    }
    sTlsTag = {};
    return kTagTampered;
}

static void nativeClearSubjectTag(JNIEnv*, jclass) {
    sTlsTag = {};
}

}  // namespace

static jstring nativeGetDciCodebaseFromListener(JNIEnv* env, jclass, jobject listener) {
    __android_log_print(ANDROID_LOG_INFO, TAG, "nativeGetDciCodebase: fn=%p",
            (void*)ArtGetDciCodebaseFromListener);
    if (ArtGetDciCodebaseFromListener == nullptr) return nullptr;
    return ArtGetDciCodebaseFromListener(env, listener);
}

static jint nativeGetDciIndexFromListener(JNIEnv* env, jclass, jobject listener) {
    __android_log_print(ANDROID_LOG_INFO, TAG, "nativeGetDciIndex: fn=%p",
            (void*)ArtGetDciIndexFromListener);
    if (ArtGetDciIndexFromListener == nullptr) return -1;
    return ArtGetDciIndexFromListener(env, listener);
}

static jstring nativeGetDciCodebaseFromMethod(JNIEnv* env, jclass, jobject method) {
    __android_log_print(ANDROID_LOG_INFO, TAG, "nativeGetDciCodebase(Method): fn=%p",
            (void*)ArtGetDciCodebaseFromReflectedMethod);
    if (ArtGetDciCodebaseFromReflectedMethod == nullptr) return nullptr;
    return ArtGetDciCodebaseFromReflectedMethod(env, method);
}

static jint nativeGetDciIndexFromMethod(JNIEnv* env, jclass, jobject method) {
    __android_log_print(ANDROID_LOG_INFO, TAG, "nativeGetDciIndex(Method): fn=%p",
            (void*)ArtGetDciIndexFromReflectedMethod);
    if (ArtGetDciIndexFromReflectedMethod == nullptr) return -1;
    return ArtGetDciIndexFromReflectedMethod(env, method);
}

// Add these entries to the class' JNINativeMethod table.
static const JNINativeMethod gDciMethods[] = {
    { "nativeWriteSubjectTag", "(I)V", (void*)nativeWriteSubjectTag },
    { "nativeVerifySubjectTag", "()I", (void*)nativeVerifySubjectTag },
    { "nativeClearSubjectTag", "()V", (void*)nativeClearSubjectTag },
    { "nativeGetDciCodebase", "(Landroid/view/View$OnClickListener;)Ljava/lang/String;",
            (void*)nativeGetDciCodebaseFromListener },
    { "nativeGetDciIndex",    "(Landroid/view/View$OnClickListener;)I",
            (void*)nativeGetDciIndexFromListener },
    { "nativeGetDciCodebase", "(Ljava/lang/reflect/Method;)Ljava/lang/String;",
            (void*)nativeGetDciCodebaseFromMethod },
    { "nativeGetDciIndex",    "(Ljava/lang/reflect/Method;)I",
            (void*)nativeGetDciIndexFromMethod },
};
