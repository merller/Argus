package com.mobicom.guibench;

import android.content.Context;
import android.view.accessibility.AccessibilityNodeInfo;
import android.widget.Button;

public class DeceptiveButton extends Button {
    private CharSequence agentVisibleLabel;
    private CharSequence cfValue;

    public DeceptiveButton(Context context) {
        super(context);
    }

    public void setAgentVisibleLabel(CharSequence label) {
        agentVisibleLabel = label;
    }

    public void setCfValue(CharSequence value) {
        cfValue = value;
    }

    @Override
    public void onInitializeAccessibilityNodeInfo(AccessibilityNodeInfo info) {
        super.onInitializeAccessibilityNodeInfo(info);
        if (agentVisibleLabel != null) {
            // Android UI dump tools read from the node info surface exposed here.
            info.setText(agentVisibleLabel);
            info.setContentDescription(agentVisibleLabel);
        }
        if (cfValue != null) {
            info.getExtras().putCharSequence("cf", cfValue);
        }
    }
}
