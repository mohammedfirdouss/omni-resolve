{{/*
Expand the name of the chart.
*/}}
{{- define "omniresolve.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "omniresolve.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart label.
*/}}
{{- define "omniresolve.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "omniresolve.labels" -}}
helm.sh/chart: {{ include "omniresolve.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: omniresolve
{{- end -}}

{{/*
Merge a service entry with serviceDefaults.
Usage: {{ $svc := include "omniresolve.serviceConfig" (dict "root" $ "svc" $svc) | fromYaml }}
*/}}
{{- define "omniresolve.serviceConfig" -}}
{{- $defaults := deepCopy .root.Values.serviceDefaults -}}
{{- $merged := mergeOverwrite $defaults (deepCopy .svc) -}}
{{- toYaml $merged -}}
{{- end -}}

{{/*
Resolve the image reference for a service.
Usage: include "omniresolve.image" (dict "root" $ "name" $name "svc" $svc)
*/}}
{{- define "omniresolve.image" -}}
{{- $repo := printf "%s/%s" .root.Values.image.registry .name -}}
{{- if and .svc.image .svc.image.repository -}}
{{- $repo = .svc.image.repository -}}
{{- end -}}
{{- $tag := .root.Values.image.tag -}}
{{- if and .svc.image .svc.image.tag -}}
{{- $tag = .svc.image.tag -}}
{{- end -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}

{{/*
Default in-cluster endpoints derived from the subchart service names.
*/}}
{{- define "omniresolve.databaseUrl" -}}
{{- if .Values.secrets.databaseUrl -}}
{{- .Values.secrets.databaseUrl -}}
{{- else -}}
{{- printf "postgresql+asyncpg://%s:%s@%s-postgresql:5432/%s" .Values.postgresql.auth.username .Values.postgresql.auth.password .Release.Name .Values.postgresql.auth.database -}}
{{- end -}}
{{- end -}}

{{- define "omniresolve.rabbitmqUrl" -}}
{{- if .Values.secrets.rabbitmqUrl -}}
{{- .Values.secrets.rabbitmqUrl -}}
{{- else -}}
{{- printf "amqp://%s:%s@%s-rabbitmq:5672/" .Values.rabbitmq.auth.username .Values.rabbitmq.auth.password .Release.Name -}}
{{- end -}}
{{- end -}}

{{- define "omniresolve.qdrantUrl" -}}
{{- if .Values.config.qdrantUrl -}}
{{- .Values.config.qdrantUrl -}}
{{- else -}}
{{- printf "http://%s-qdrant:6333" .Release.Name -}}
{{- end -}}
{{- end -}}

{{- define "omniresolve.aiGatewayUrl" -}}
{{- if .Values.config.aiGatewayUrl -}}
{{- .Values.config.aiGatewayUrl -}}
{{- else -}}
{{- printf "http://%s-ai-gateway:%d" .Release.Name (int (dig "service" "port" 8100 (index .Values.services "ai-gateway"))) -}}
{{- end -}}
{{- end -}}

{{/*
Name of the shared secret consumed by every service.
*/}}
{{- define "omniresolve.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-env" (include "omniresolve.fullname" .) -}}
{{- end -}}
{{- end -}}
