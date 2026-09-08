package com.mobicom.guibench;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;

public class MainActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Intent intent = new Intent(this, ScenarioActivity.class);
        intent.putExtra(ScenarioIds.EXTRA_SCENARIO, ScenarioIds.HOME_ALL);
        intent.putExtra(ScenarioIds.EXTRA_DELAY_MS, 1800L);
        startActivity(intent);
        finish();
    }
}
