#define LOG_TAG "AccessibilityClickMarker"
#include <jni.h>
#include <android/dlext.h>
#include <dlfcn.h>
#include <string.h>
#include <string>
#include <log/log.h>
#include <nativehelper/JNIHelp.h>
#include <algorithm>
#include <android/log.h>
#define TAG "DCI_NATIVE"

#ifndef UNLIKELY
#define UNLIKELY(x) __builtin_expect(!!(x), 0)
#endif

extern "C" android_namespace_t* android_get_exported_namespace(const char* name);

namespace android {

using FnThreadCurrent = void*(*)();
using FnSetCustomTLS  = void(*)(void* thread, const char* key, void* data);

struct AgentSubject {
    void*    vtable_ptr;
    uint32_t source;
    uint32_t padding;
    uint8_t  domain[32];
};

struct LibArt {
    FnThreadCurrent  thread_current = nullptr;
    FnSetCustomTLS   set_custom_tls = nullptr;
    const char*      tls_key       = nullptr;
    bool             ready         = false;
};

static void* OpenLoadedLibArt() {
    // ART is loaded into its exported APEX linker namespace, not the platform
    // namespace containing libandroid_runtime.  A plain dlopen/weak reference
    // therefore cannot see it from this library.
    android_namespace_t* artNamespace =
            android_get_exported_namespace("com_android_art");
    if (artNamespace != nullptr) {
        android_dlextinfo info = {};
        info.flags = ANDROID_DLEXT_USE_NAMESPACE;
        info.library_namespace = artNamespace;
        void* handle = android_dlopen_ext(
                "libart.so", RTLD_NOW | RTLD_NOLOAD, &info);
        if (handle != nullptr) {
            return handle;
        }
    }
    return dlopen("libart.so", RTLD_NOW | RTLD_NOLOAD);
}

static LibArt& GetLibArt() {
    static LibArt s;
    if (UNLIKELY(!s.ready)) {
        void* libart = OpenLoadedLibArt();
        if (libart == nullptr) return s;
        s.thread_current = reinterpret_cast<FnThreadCurrent>(
            dlsym(libart, "_ZN3art6Thread7CurrentEv"));
        s.set_custom_tls = reinterpret_cast<FnSetCustomTLS>(
            dlsym(libart, "_ZN3art6Thread12SetCustomTLSEPKcPNS_7TLSDataE"));
        const char** key_sym = reinterpret_cast<const char**>(
            dlsym(libart, "kAgentSubjectTlsKey"));
        if (key_sym != nullptr) s.tls_key = *key_sym;
        s.ready = (s.thread_current != nullptr &&
                   s.set_custom_tls != nullptr &&
                   s.tls_key        != nullptr);
    }
    return s;
}

static void nativeSetAgentSubject(JNIEnv* env, jclass, jint source, jbyteArray domain) {
    LibArt& art = GetLibArt();
    if (!art.ready) return;
    void* self = art.thread_current();
    if (self == nullptr) return;

    AgentSubject* subj = new AgentSubject();
    subj->vtable_ptr = nullptr;
    subj->source     = static_cast<uint32_t>(source);
    subj->padding    = 0;
    memset(subj->domain, 0, 32);
    if (domain != nullptr) {
        jsize len = env->GetArrayLength(domain);
        if (len > 32) len = 32;
        env->GetByteArrayRegion(domain, 0, len,
            reinterpret_cast<jbyte*>(subj->domain));
    }
    art.set_custom_tls(self, art.tls_key, subj);
}

static void nativeClearAgentSubject(JNIEnv*, jclass) {
    LibArt& art = GetLibArt();
    if (!art.ready) return;
    void* self = art.thread_current();
    if (self == nullptr) return;
    art.set_custom_tls(self, art.tls_key, nullptr);
}




// 文件顶部加这两行弱符号声明
__attribute__((weak)) extern "C" jstring ArtGetDciCodebaseFromListener(JNIEnv*, jobject);
__attribute__((weak)) extern "C" jint    ArtGetDciIndexFromListener(JNIEnv*, jobject);

using FnArgusDciResolveAndLog = jint(*)(
        JNIEnv*, jobject, jstring, jstring, jstring, jint, jlong, jint, jint,
        jstring, jstring, jstring, jstring);
using FnArgusContinuationBegin = jlong(*)(
        JNIEnv*, jobject, jstring, jstring, jobject, jstring, jstring, jstring,
        jint, jint, jstring, jstring, jstring);
using FnArgusContinuationEnd = jint(*)(jlong);

static FnArgusDciResolveAndLog GetArgusDciResolver() {
    // libandroid_runtime does not have a DT_NEEDED dependency on libart.  A weak
    // reference therefore stays null even though ART exports the symbol and is
    // already loaded by the process.  Resolve it from the loaded ART handle,
    // matching the bridge used above for the existing ART helpers.
    static const FnArgusDciResolveAndLog resolver = []() {
        void* libart = OpenLoadedLibArt();
        if (libart == nullptr) {
            return static_cast<FnArgusDciResolveAndLog>(nullptr);
        }
        return reinterpret_cast<FnArgusDciResolveAndLog>(
                dlsym(libart, "ArtArgusDciResolveAndLog"));
    }();
    return resolver;
}

static FnArgusContinuationBegin GetArgusContinuationBegin() {
    static const FnArgusContinuationBegin begin = []() {
        void* libart = OpenLoadedLibArt();
        if (libart == nullptr) {
            return static_cast<FnArgusContinuationBegin>(nullptr);
        }
        return reinterpret_cast<FnArgusContinuationBegin>(
                dlsym(libart, "ArtArgusContinuationBegin"));
    }();
    return begin;
}

static FnArgusContinuationEnd GetArgusContinuationEnd() {
    static const FnArgusContinuationEnd end = []() {
        void* libart = OpenLoadedLibArt();
        if (libart == nullptr) {
            return static_cast<FnArgusContinuationEnd>(nullptr);
        }
        return reinterpret_cast<FnArgusContinuationEnd>(
                dlsym(libart, "ArtArgusContinuationEnd"));
    }();
    return end;
}

// 同时把 ensureFns、gGetDciCodebaseFn、gGetDciIndexFn 相关代码全部删掉

static jstring nativeGetDciCodebase(JNIEnv* env, jclass, jobject listener) {
    __android_log_print(ANDROID_LOG_INFO, TAG, "nativeGetDciCodebase: fn=%p",
        (void*)ArtGetDciCodebaseFromListener);
    if (ArtGetDciCodebaseFromListener == nullptr) return nullptr;
    return ArtGetDciCodebaseFromListener(env, listener);
}

static jint nativeGetDciIndex(JNIEnv* env, jclass, jobject listener) {
    __android_log_print(ANDROID_LOG_INFO, TAG, "nativeGetDciIndex: fn=%p",
        (void*)ArtGetDciIndexFromListener);
    if (ArtGetDciIndexFromListener == nullptr) return -1;
    return ArtGetDciIndexFromListener(env, listener);
}



static const JNINativeMethod gMethods[] = {
    { "nativeSetAgentSubject",   "(I[B)V", (void*)nativeSetAgentSubject   },
    { "nativeClearAgentSubject", "()V",    (void*)nativeClearAgentSubject },
    { "nativeGetDciCodebase", "(Landroid/view/View$OnClickListener;)Ljava/lang/String;", (void*)nativeGetDciCodebase },
    { "nativeGetDciIndex",    "(Landroid/view/View$OnClickListener;)I",                  (void*)nativeGetDciIndex    },
};

int register_android_view_AccessibilityClickMarker(JNIEnv* env) {
    return jniRegisterNativeMethods(
        env,
        "android/view/AccessibilityClickMarker",
        gMethods,
        NELEM(gMethods));
}

static jint nativeArgusDciResolveAndLog(JNIEnv* env, jclass, jobject callback,
        jstring methodName, jstring descriptor, jstring domain, jint targetId,
        jlong targetToken, jint inputEventId, jint inputSource, jstring operation,
        jstring phase, jstring route, jstring argumentsHash) {
    const FnArgusDciResolveAndLog resolver = GetArgusDciResolver();
    if (resolver == nullptr) {
        ALOGE("ART DCI-plus resolver is unavailable");
        return -1;
    }
    return resolver(env, callback, methodName, descriptor, domain,
            targetId, targetToken, inputEventId, inputSource, operation, phase, route,
            argumentsHash);
}

static jlong nativeArgusContinuationBegin(JNIEnv* env, jclass, jobject callback,
        jstring methodName, jstring descriptor, jobject bindingCallback,
        jstring bindingMethodName, jstring bindingDescriptor, jstring domain,
        jint inputEventId, jint inputSource, jstring operation, jstring route,
        jstring argumentsHash) {
    const FnArgusContinuationBegin begin = GetArgusContinuationBegin();
    if (begin == nullptr) {
        ALOGE("ART continuation begin gate is unavailable");
        return -1;
    }
    return begin(env, callback, methodName, descriptor, bindingCallback, bindingMethodName,
            bindingDescriptor, domain, inputEventId, inputSource, operation, route,
            argumentsHash);
}

static jint nativeArgusContinuationEnd(JNIEnv*, jclass, jlong token) {
    const FnArgusContinuationEnd end = GetArgusContinuationEnd();
    if (end == nullptr) {
        ALOGE("ART continuation end gate is unavailable");
        return -1;
    }
    return end(token);
}

static const JNINativeMethod gArgusDciMethods[] = {
    { "nativeResolveAndLog",
      "(Ljava/lang/Object;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;IJIILjava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;)I",
      (void*)nativeArgusDciResolveAndLog },
    { "nativeBeginContinuation",
      "(Ljava/lang/Object;Ljava/lang/String;Ljava/lang/String;Ljava/lang/Object;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;IILjava/lang/String;Ljava/lang/String;Ljava/lang/String;)J",
      (void*)nativeArgusContinuationBegin },
    { "nativeEndContinuation", "(J)I", (void*)nativeArgusContinuationEnd },
};

int register_android_view_ArgusDci(JNIEnv* env) {
    return jniRegisterNativeMethods(env, "android/view/ArgusDci", gArgusDciMethods,
            NELEM(gArgusDciMethods));
}

} // namespace android
