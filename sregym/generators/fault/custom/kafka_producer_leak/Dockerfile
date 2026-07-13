FROM maven:3.9-eclipse-temurin-11 AS build
WORKDIR /app
COPY pom.xml .
COPY src ./src
RUN mvn package -q

FROM eclipse-temurin:11-jre
WORKDIR /app
COPY --from=build /app/target/producer-leak-1.0-jar-with-dependencies.jar app.jar
ENTRYPOINT ["java", "-jar", "app.jar"]