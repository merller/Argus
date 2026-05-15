package com.example.MobileGPT;

import java.util.Arrays;
import java.util.List;

public class MobileGPTGlobal {
    // For Android emulator, 10.0.2.2 points to the host machine running the server.
    public static final String HOST_IP = "10.0.2.2";
    public static final int HOST_PORT = 12345;
    public static final String STRING_ACTION = "com.example.MobileGPT.STRING_ACTION";
    public static final String INSTRUCTION_EXTRA = "com.example.MobileGPT.INSTRUCTION_EXTRA";
    public static final String APP_NAME_EXTRA = "com.example.MobileGPT.APP_NAME_EXTRA";

    private static MobileGPTGlobal sInstance = null;

    public static final List<String> AVAILABLE_ACTIONS = Arrays.asList("click", "input", "scroll", "long-click", "go-back");

    protected MobileGPTGlobal() {

    }

    public static synchronized MobileGPTGlobal getInstance() {
        if (sInstance == null) {
            sInstance = new MobileGPTGlobal();
        }
        return sInstance;
    }

    public static synchronized MobileGPTGlobal reset() {
        sInstance = new MobileGPTGlobal();
        return sInstance;
    }
}
